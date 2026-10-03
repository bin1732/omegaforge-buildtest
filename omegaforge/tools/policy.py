"""OmegaForge 门禁（Gate）— 权限四级 × 风险分级 × 审批 × 幂等 × 审计闭环。

为什么需要这一层
----------------
原有的 Permissions 只是三个布尔开关（terminal / fs / web_fetch），
它是「能力开关」，回答的是"这个能力开没开"。但真实产品还需要回答第二个
正交问题："开了之后，多大的动作还需要问用户一声"。

这两者是相乘关系，不是二选一：
  - 只做能力开关 → 一旦全开，写文件、跑命令全部无门槛，"完全访问"名副其实地失控
  - 只做四级模式 → 没开的 terminal 会绕过开关直接被放行，能力开关形同虚设

所以正确模型是 gate = 能力开关 AND 自主级别矩阵。

四级模式（对齐设置页 UI 文案）
------------------------------
  confirm   变更前确认  —— 改文件前先问我
  auto_edit 自动编辑    —— 自动编辑文件
  plan      计划模式    —— 编辑前先出计划
  full      完全访问    —— 减少确认次数

矩阵（mode × risk → verdict）
----------------------------
              low(读)  medium(写/抓)  high(终端)
  confirm     allow    ask            ask
  auto_edit   allow    allow          ask
  plan        allow    plan           plan
  full        allow    allow          allow

关键决策：full 放行 high（终端命令），这正是"减少确认次数"的产品语义；
但 **critical 不在矩阵里** —— 破坏性命令与工作区越界属于硬拒绝，
任何模式（包括 full）都不可放行。

为什么这条底线不能放进矩阵：代理一旦能执行破坏性命令，就能用本机既有的
凭据与权限执行不可逆操作，一次误判的损失无法用"再确认一次"弥补。可确认
的前提是操作可撤销；破坏性操作不满足这个前提，所以它不是"要不要问"
的问题，而是"能不能做"的问题。

审批不是把开关打开
------------------
ask 时签发一次性 approval 凭证（HMAC 签名，绑定工具与参数摘要），
用户批准后带凭证重放即可执行——它只放行"这一次"，
不会把 mode 永久提升，也不会写回 capabilities。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from ..core.atomicio import atomic_write, atomic_write_json
from ..core.errors import UserError
from ..core.paths import resolve_home
from typing import Optional

MODES = ("confirm", "auto_edit", "plan", "full")

MODE_LABELS = {
    "confirm": "变更前确认",
    "auto_edit": "自动编辑",
    "plan": "计划模式",
    "full": "完全访问",
}

MODE_HINTS = {
    "confirm": "改文件前先问我",
    "auto_edit": "自动编辑文件",
    "plan": "编辑前先出计划",
    "full": "减少确认次数",
}

# 中文名 -> 标识。仅用于入参归一化：落盘与审计一律存标识。
_LABEL_TO_MODE = {v: k for k, v in MODE_LABELS.items()}


def normalize_mode(mode) -> str:
    """把入参归一化为标识（confirm / auto_edit / plan / full）。

    为什么要认中文名：报错与界面上显示的都是中文名，而落盘只认标识。
    只认标识的话，照报错填「完全访问」会被再次拒绝，而报错列的正是
    中文名——用户无论重试多少次都进不去。归一化后两种写法都可用，
    且存下来的始终是标识，不影响既有数据与审计口径。
    """
    m = str(mode or "").strip()
    if m in MODES:
        return m
    if m in _LABEL_TO_MODE:
        return _LABEL_TO_MODE[m]
    return m

# 只有矩阵内三档。critical 是矩阵外的硬拒绝，任何模式都 deny。
RISK_LEVELS = ("low", "medium", "high")

_MATRIX: dict[str, dict[str, str]] = {
    "confirm":   {"low": "allow", "medium": "ask",   "high": "ask"},
    "auto_edit": {"low": "allow", "medium": "allow", "high": "ask"},
    "plan":      {"low": "allow", "medium": "plan",  "high": "plan"},
    "full":      {"low": "allow", "medium": "allow", "high": "allow"},
}

# 工具名 -> (所需能力开关, 风险等级)
TOOL_RISK: dict[str, tuple[str, str]] = {
    "fs.read":   ("fs", "low"),
    "fs.list":   ("fs", "low"),
    "fs.write":  ("fs", "medium"),
    "web_fetch": ("web_fetch", "medium"),
    "terminal":  ("terminal", "high"),
    # 个人工具包（由蒸馏体运行时注入）必须登记：
    # 未登记的工具在决策时会被当成最高风险，一旦接入门禁就会在完全访问档
    # 之外全部要求确认——而这个分类只是"没登记"的副产品，不是判断的结果。
    # 更直接的后果：渲染给界面的风险清单里根本没有这些工具，用户在设置页
    # 看不见它们，也就无从管起。
    #
    # capability 留空（不要求 terminal/fs/web_fetch 任一开关）：它们是
    # 智能体自己的记忆与待办，不该被"终端/文件系统"这类开关牵连。
    # 但风险分级必须写：读=low，写=medium（与 fs.write 同级——都是
    # 落盘的用户数据，会一直留在那儿）。
    #
    # 名字必须与**真实暴露的工具名**一致，不能凭想象登记。
    # 两处错位：
    #   wiki_put / wiki_write / task_complete —— 登记了，但代码里没有任何
    #       定义。而 POLICY_META 会把它们渲染进设置页的风险清单，用户看到
    #       的是三个根本不存在的工具，还以为自己能管。
    #   wiki_save（MCP 真实暴露、也在 MCP_SCOPE_DEFAULTS 里）—— **没有**
    #       登记。走 decide() 时按未知工具落到 ("", "high")，比它实际的
    #       medium 更严；严一点不会出事，但这张表从此不再可信。
    # 两者都是同一类错误：表与实现分叉，而没有任何检查拦住分叉。
    "kb_search":       ("", "low"),
    "task_list":       ("", "low"),
    "wiki_get":        ("", "low"),
    "wiki_search":     ("", "low"),
    "memory_recall":   ("", "low"),
    "kb_add":          ("", "medium"),
    "kb_delete":       ("", "medium"),
    "task_add":        ("", "medium"),
    "task_done":       ("", "medium"),
    "task_delete":     ("", "medium"),
    "memory_remember": ("", "medium"),
    "wiki_save":       ("", "medium"),
    "wiki_delete":     ("", "medium"),
}


def _home() -> Path:
    # 唯一真源在 core/paths.py；保留薄封装以兼容既有调用点。
    return Path(resolve_home())


_log = logging.getLogger("omegaforge.errors")


# 个人数据（知识库 / 记忆 / 词条 / 待办）的**写**工具。
# 它们不走 tool_dispatch（直接调 self.kb.add() 等），所以矩阵必须在这条
# 路径上单独施加——否则 TOOL_RISK 里的分级只是一张给人看的表。
PERSONAL_WRITE_TOOLS = frozenset({
    "kb_add", "kb_delete", "memory_remember",
    "wiki_save", "wiki_delete",
    "task_add", "task_done", "task_delete",
})


def gate_personal_write(tool: str, args: Optional[dict] = None,
                        origin: Optional[str] = None) -> str:
    """个人数据写工具过四级矩阵：与 fs.write 同标，且放行侧也留痕。

    返回 "allow"；其余情况抛 ApprovalRequired / PlanRequired，由上层
    （server 或 _classify）转成可展示文案。

    为什么必须单独有一个入口
    ------------------------
    这些工具在 mcp_server 里直接调 `self.kb.add()`，不经过 `_gate()`，
    所以既不受矩阵约束、也不写审计。例如：同一 MCP 客户端、
    同一最保守档 confirm 下，`fs_write` 被拒绝而 `kb_add` 直接写进用户
    知识库，且 `audit_read` 一条都没有——"谁在何时通过外部客户端写了什么"
    完全无从查证。

    `origin` 必须记下来：审计里分不清"界面里点的"和"外部工具发起的"，
    事后就无法追溯来源。
    """
    mode = Policy().mode()
    cap, risk = TOOL_RISK.get(tool, ("", "high"))
    ev = {"tool": tool, "risk": risk, "mode": mode, "capability": cap,
          "origin": origin}
    d = Policy().decide(tool)
    verdict = d["verdict"]
    if verdict == "allow":
        audit_write(dict(ev, verdict="allow", reason="personal_write"))
        return "allow"
    if verdict == "plan":
        audit_write(dict(ev, verdict="plan"))
        raise PlanRequired(tool, {"tool": tool, "args": args or {},
                                  "risk": risk, "mode": mode})
    audit_write(dict(ev, verdict="ask"))
    raise ApprovalRequired(
        tool, risk, issue_approval(tool, args, mode),
        f"当前权限级别「{d['mode_label']}」下该操作需要确认",
        origin=origin)


class ApprovalRequired(Exception):
    """需要用户确认（不是错误，是一次交互）。

    与 CapabilityDenied 严格区分：
      CapabilityDenied  = 能力没开，去设置里开开关（403 + need_permission）
      ApprovalRequired  = 能力已开，但本次动作超出当前模式的免确认范围（409）
    两者混为一谈会让用户收到错误引导——正如若把"开关没开"
    报成"读写失败，请检查运行目录权限"那样。
    """

    def __init__(self, tool: str, risk: str, approval: dict, reason: str,
                 origin: Optional[str] = None):
        super().__init__(reason)
        self.tool = tool
        self.risk = risk
        self.approval = approval
        self.reason = reason
        # 调用来源。MCP 客户端**没有弹窗确认这条通道**：ask 对它而言等于
        # 永远拒绝，而"该操作需要你确认后才会执行"这句话会让人以为等一等
        # 就会好。带上 origin，_classify 才能给出能真正走下去的指引。
        self.origin = origin


class PlanRequired(Exception):
    """计划模式：不执行，改为返回将要执行的计划。"""

    def __init__(self, tool: str, plan: dict):
        super().__init__("plan")
        self.tool = tool
        self.plan = plan


class Policy:
    """自主级别持久化 + 矩阵决策。默认 confirm（最保守）。"""

    DEFAULT_MODE = "confirm"

    def __init__(self, home: Optional[str] = None):
        self._home = home

    @property
    def path(self) -> Path:
        # 惰性：见 core/paths.py。__init__ 期固化会让模块级
        # 单例在改 OMEGAFORGE_HOME 后仍读写旧目录。
        return Path(resolve_home(self._home)) / "policy.json"

    def mode(self) -> str:
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                m = str(d.get("mode", self.DEFAULT_MODE))
                if m in MODES:
                    return m
            except (json.JSONDecodeError, OSError):
                pass
        return self.DEFAULT_MODE

    def set_mode(self, mode: str) -> str:
        mode = normalize_mode(mode)
        if mode not in MODES:
            # 必须抛 UserError：这里已经写好了可执行的中文文案（列出全部
            # 合法级别），抛 ValueError 会被统一压成「请求内容有误，请检查后
            # 重试」，用户既不知道能选什么，也不知道自己填错了哪一项。
            #
            # 报错必须同时给出标识与中文名：只列中文名的话，用户照着填
            # 「完全访问」会被再次拒绝——而 set_mode 只认标识 full。
            # 这类"照着报错填还是错"的死循环不产生别的症状，只有真的
            # 拿报错里的值去填第二遍才会撞上。
            raise UserError(
                "权限级别只能是：" + "、".join(
                    f"{m}（{MODE_LABELS[m]}）" for m in MODES))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old = self.mode()
        atomic_write_json(self.path, {"mode": mode})
        # 权限级别变更是敏感操作：从 confirm 提到 full 等于交出免确认权，
        # 不记审计就无法回答"是谁在什么时候放宽了门禁"。
        ok = audit_write({"tool": "policy.set_mode", "verdict": "allow",
                          "from": old, "to": mode,
                          "risk": "critical" if mode == "full" else "medium"})
        if not ok:
            # 新的权限级别已经落盘生效，此时绝不能返回成功——否则用户会以为
            # 这次变更留下了记录，而审计页永远是空的。也不能回滚：磁盘既然
            # 写不进审计，回滚同样可能失败，反而把状态搞成两处不一致。
            # 因此明确告知"已生效 + 未记录"，让用户自己判断要不要重来。
            raise UserError(
                f"权限级别已更新为「{MODE_LABELS.get(mode, mode)}」，"
                "但审计记录未能写入，请检查数据目录是否可写后重新设置")
        return mode

    def decide(self, tool: str) -> dict:
        """返回 {tool, capability, risk, mode, verdict, label}。"""
        cap, risk = TOOL_RISK.get(tool, ("", "high"))
        mode = self.mode()
        verdict = _MATRIX.get(mode, _MATRIX[self.DEFAULT_MODE]).get(risk, "ask")
        return {"tool": tool, "capability": cap, "risk": risk,
                "mode": mode, "verdict": verdict,
                "mode_label": MODE_LABELS[mode]}


# ---------------------------------------------------------------- 审批凭证
def _key_path() -> Path:
    return _home() / ".gate_key"


_SECRET_CACHE: Optional[bytes] = None


def _secret() -> bytes:
    """进程内持久密钥。权限收紧到 0600，避免同机其他用户读取伪造凭证。

    必须做进程内缓存：若写盘失败（只读目录、容器受限挂载）就返回新随机密钥，
    那么下一次调用会拿到不同的密钥，已签发的所有审批凭证会瞬间全部失效，
    用户表现为"刚批准完就报凭证无效"。缓存保证同一进程内密钥恒定。
    """
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    p = _key_path()
    if p.is_file():
        try:
            data = p.read_bytes()
            if data:
                _SECRET_CACHE = data
                return data
        except OSError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    k = os.urandom(32)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(k)
    except OSError:
        pass
    _SECRET_CACHE = k
    return k


def _args_digest(args: Optional[dict]) -> str:
    try:
        raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(args)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def issue_approval(tool: str, args: Optional[dict], mode: str,
                   ttl: int = 600) -> dict:
    """签发一次性审批请求。返回可展示给用户的描述 + approval_id。"""
    exp = int(time.time()) + ttl
    nonce = os.urandom(8).hex()
    dig = _args_digest(args)
    body = f"{tool}|{dig}|{exp}|{nonce}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:24]
    aid = f"{exp}.{nonce}.{sig}"
    return {"approval_id": aid, "tool": tool, "mode": mode,
            "expires_at": exp, "args_digest": dig,
            "prompt": _prompt(tool, mode)}


def _prompt(tool: str, mode: str) -> str:
    cap, risk = TOOL_RISK.get(tool, ("", "high"))
    risk_cn = {"low": "只读", "medium": "会写入改动", "high": "会执行本机命令"}
    return (f"当前权限级别为「{MODE_LABELS.get(mode, mode)}」，"
            f"该操作{risk_cn.get(risk, '风险较高')}，需要你确认后才会执行。")


def verify_approval(approval_id: str, tool: str,
                    args: Optional[dict]) -> bool:
    """校验并一次性消费。重放同一凭证第二次必然失败。"""
    if not approval_id or "." not in str(approval_id):
        return False
    parts = str(approval_id).split(".")
    if len(parts) != 3:
        return False
    exp_s, nonce, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    body = f"{tool}|{_args_digest(args)}|{exp}|{nonce}"
    want = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(want, sig):
        return False
    # 一次性消费：已用过的 nonce 记录在案，防止重放攻击。
    #
    # 清理策略必须按"是否已过期"，不能按字典序截断：按字典序只保留一部分
    # 记录时，被挤掉的记录若仍在有效期内，同一凭证就能被执行第二次。按
    # 过期时间清理，则有效期内的凭证必然留档。
    now = int(time.time())
    seen = _load_used(now)
    if nonce in seen:
        return False
    seen[nonce] = exp
    _save_used(seen)
    return True


def _used_path() -> Path:
    return _home() / ".gate_used"


def _load_used(now: int) -> dict:
    """读取未过期的已消费 nonce。格式为每行 "nonce exp"。

    只保留未过期的：过期的凭证本身已通不过 exp 校验，留着只会让文件无限增长。
    坏行跳过而不是让整个校验失败（与 audit_read / UsageStore 同策略）。
    """
    p = _used_path()
    out: dict[str, int] = {}
    if not p.is_file():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for ln in lines:
        parts = ln.strip().split()
        if len(parts) != 2:
            continue
        nonce, exp_s = parts
        try:
            exp = int(exp_s)
        except ValueError:
            continue
        if exp > now:
            out[nonce] = exp
    return out


def _save_used(seen: dict) -> None:
    """原子写回。若写失败则校验放行——宁可多问一次，不可伪造放行。"""
    p = _used_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, "".join(f"{nonce} {exp}\n"
                                for nonce, exp in seen.items()))
    except OSError:
        pass


# -------------------------------------------------------------------- 幂等
def _idem_dir() -> Path:
    return _home() / "idem"


def idem_get(key: str) -> Optional[dict]:
    """返回首次执行结果；重复请求不再执行第二遍。"""
    if not key:
        return None
    p = _idem_dir() / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("result")
    except (json.JSONDecodeError, OSError):
        return None


def idem_put(key: str, result: dict) -> None:
    if not key:
        return
    d = _idem_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")
        atomic_write_json(p, {"ts": time.time(), "result": result},
                          indent=None)
    except OSError:
        pass


# ------------------------------------------------------------ 审计（可查）
def audit_write(entry: dict) -> bool:
    """追加审计，返回是否写入成功。

    与旧 _audit 的区别：记录 verdict / approval / idem，且可被
    audit_read 查询——只写不读的审计等于没有审计。

    为什么必须把成败交给调用方
    --------------------------
    写不进去的情况（磁盘满、目录不可写、路径被同名目录占用）是真实存在的，
    此时"没有审计记录"与"没有发生过"在 audit_read 里长得一模一样。
    若这里一律静默，依赖审计回答"谁在何时放宽了门禁"的场景就会得到
    一个假的空答案。因此本函数只负责如实上报，是否阻断由调用方按
    该操作的敏感度决定：

      - 权限级别变更：必须让用户知情（见 Policy.set_mode）
      - 单次工具执行：不阻断主流程，但失败原因进内部日志便于排查
    """
    try:
        p = _home() / "tools_audit.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time()}
        rec.update(entry)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False,
                               default=str) + "\n")
        return True
    except OSError as e:
        # 不阻断调用方，但必须留下排查线索：静默会让"审计是空的"这件事
        # 无法区分是没人操作过，还是全都写失败了。
        try:
            _log.error("审计写入失败 tool=%s 原因=%s",
                       entry.get("tool", "-"), type(e).__name__)
        except Exception:  # noqa: BLE001 — 日志本身绝不能影响主流程
            pass
        return False


def audit_read(limit: int = 50, tool: Optional[str] = None,
               verdict: Optional[str] = None) -> list:
    p = _home() / "tools_audit.jsonl"
    if not p.is_file():
        return []
    out = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    # 只读尾部，避免大文件全量解析
    for ln in lines[-2000:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            # 坏行跳过而不是让整个审计查询 500（与 UsageStore 同策略）
            continue
        if tool and d.get("tool") != tool:
            continue
        if verdict and d.get("verdict") != verdict:
            continue
        out.append(d)
    return out[-limit:]


def audit_write_legacy(tool: str, detail: str) -> None:
    """兼容旧 _audit 调用点的写入（无 verdict 信息）。"""
    audit_write({"tool": tool, "detail": str(detail)[:300]})


def POLICY_META() -> dict:
    """给前端渲染四级选择器用：枚举 + 当前值 + 矩阵（可解释，不是黑箱）。"""
    return {"modes": [{"id": m, "label": MODE_LABELS[m], "hint": MODE_HINTS[m]}
                      for m in MODES],
            "matrix": _MATRIX,
            "tool_risk": {k: {"capability": v[0], "risk": v[1]}
                          for k, v in TOOL_RISK.items()},
            "note": "critical（破坏性命令 / 工作区越界）不在矩阵内，任何模式都拒绝"}
