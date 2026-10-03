"""聚合上下文评估：把评估单元从「单条消息」升级为「会话工作上下文总和」。

为什么需要它
------------
门禁 `_dangerous()` 只回答「这一条命令危不危险」。但攻击者不需要一次
性递上危险命令——下面这条链在「完全访问」模式下 **四步全部放行**：

    fs_write  part1.txt = "rm -rf "
    fs_write  part2.txt = "/tmp/target"
    terminal  cat part1.txt part2.txt > combined.sh     -> 只是拼接文件
    terminal  bash combined.sh                          -> 只是执行脚本

每一步单看都合规：写的是文本、cat 是读取、bash 执行的是"自己工作区里的
脚本"。合起来等价于 `rm -rf /tmp/target`。单消息粒度的门禁在结构上
看不见这种攻击，因为「危险」不在任何一条消息里，而在**消息的累积**里。

这就是小花 MCP 架构里的 GhostSplice / ShareLock 要防的东西，也是
2026-09 开源生态的空白位。

设计取舍
--------
只做「写后执行」这一条主线，不做意图漂移评分这类需要模型的判定。
理由：本地桌面工具不能依赖外部模型做安全裁决——模型不可用时的默认
行为必须是"放行"（否则正常用法被卡死），而安全裁决 fail-open 是错的。
用确定性规则换取：可离线、可测试、可解释。

边界（明确不做）
----------------
- 不做跨会话持久化：会话结束即清。跨会话判定需要身份模型，当前没有。
- 不做内容写入阻断：写 `rm -rf /` 到 .md 文件是合法需求（写文档）。
  只在"被当作脚本执行"时拦截——**执行意图**才是危险信号，不是文本本身。
- 不做无限增长：ledger 有条目上限，超限丢弃最旧的。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..core.atomicio import atomic_write_json
from ..core.paths import resolve_home
from typing import Optional

# 会话内保留的最大贡献条目数。超过则丢弃最旧的——
# 它是"近期上下文"，不是审计链，不需要全量留存（审计由 policy.audit_write 负责）。
MAX_ENTRIES = 200

# 执行命令时，哪些程序会把文件"当作脚本/代码"执行
_EXEC_PROGS = ("bash", "sh", "zsh", "dash", "ksh", "python", "python3",
               "python2", "perl", "ruby", "php", "node", "source", ".")


def _home() -> Path:
    # 唯一真源在 core/paths.py；保留薄封装以兼容既有调用点。
    return Path(resolve_home())


class ContextLedger:
    """会话级工作上下文总账。

    与审计链的分工：
      audit_write  —— 不可篡改的历史，回答"发生过什么"（合规）
      ContextLedger —— 可丢弃的近期状态，回答"现在累积到什么程度"（防御）
    两者绝不能合并：审计要全量留存，聚合要滚动窗口，混在一起必然互相拖累。
    """

    def __init__(self, home: Optional[str] = None):
        self._home = home

    @property
    def home(self) -> Path:
        # 惰性：见 core/paths.py
        return Path(resolve_home(self._home))

    @home.setter
    def home(self, v) -> None:
        self._home = str(v) if v else None

    @property
    def path(self) -> Path:
        return self.home / "context_ledger.json"

    # -- 写入 ----------------------------------------------------------
    def add(self, kind: str, key: str, text: str, risk: bool = False) -> None:
        """记录一次贡献。kind: write / exec / read / fetch。"""
        entries = self._load()
        entries.append({
            "ts": int(time.time()),
            "kind": kind,
            "key": str(key)[:300],
            "len": len(text or ""),
            "risk": bool(risk),
        })
        # 只留最近 MAX_ENTRIES 条（滚动窗口，不是截断审计）
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        self._save(entries)

    def _load(self) -> list:
        """读取侧：账本损坏必须退化为"无历史"，绝不连累调用方。

        为什么必须显式兜 UnicodeDecodeError（见下）：
        `UnicodeDecodeError` 是 **ValueError 的子类，不是 OSError**，
        所以只捕获 JSON 解析错误与 OSError 接不住它。

        后果（比"读不到历史"严重得多）：

            账本里出现一个非 UTF-8 字节
            → ContextLedger.add() 抛 UnicodeDecodeError
            → 而 add() 在 **工具执行路径上**被调用
              （system_tools.py 的 exec 分支，每条命令都记一笔）
            → 整个工具调用连带失败

        也就是说：**一个损坏的账本文件让所有工具调用全挂**，而这个文件
        用户没有任何清理入口。

        本文件自己的取舍写得很清楚：「聚合状态写不进去不该阻断主流程——
        它只是增强，缺失时退化为'单条判定'，而绝不能让工具不可用」。
        但这个取舍只做在了写入侧（`_save`），读取侧漏了。这里补齐。

        退化为 [] 会丢掉累积风险判定的历史（"危险不在任何一条消息里，
        只存在于累积结果中"），这是**有意的取舍**：账本已损坏时，
        宁可失去累积判定能力，也不能让工具全部不可用。
        """
        try:
            if self.path.is_file():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, list):
                    return d
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        return []

    def _save(self, entries: list) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, entries, indent=None)
        except OSError:
            # 聚合状态写不进去不该阻断主流程——它只是增强，
            # 缺失时退化为"单条判定"，而绝不能让工具不可用。
            pass

    def stats(self) -> dict:
        entries = self._load()
        return {
            "entries": len(entries),
            "risk_entries": sum(1 for e in entries if e.get("risk")),
            "writes": sum(1 for e in entries if e.get("kind") == "write"),
            "execs": sum(1 for e in entries if e.get("kind") == "exec"),
        }

    def reset(self) -> None:
        try:
            if self.path.is_file():
                self.path.unlink()
        except OSError:
            pass


def script_targets(cmd: str) -> list[str]:
    """从命令中取出"会被当作脚本执行"的文件路径。

    只认 `bash x.sh` / `./x.sh` / `python x.py` 这类显式执行，
    不认 `cat x.sh`（读取不是执行意图）。判据是程序名，不是文件后缀——
    后缀可伪造（evil.txt 照样能被 bash 执行）。
    """
    norm = re.sub(r"\s+", " ", str(cmd or "").strip())
    out: list[str] = []
    for seg in re.split(r"\s*[;&]\s*", norm):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split()
        head = os.path.basename(parts[0]).lower()
        if head in _EXEC_PROGS:
            for tok in parts[1:]:
                if tok.startswith("-"):
                    continue
                out.append(tok)
                break
        elif seg.startswith("./") or seg.startswith(".\\"):
            out.append(parts[0])
    return out


def nested_script_refs(body: str) -> list[str]:
    """取脚本正文里被再次引用的脚本路径（source / bash / ./ 等）。

    攻击链不一定要把危险内容放在被执行的那一个文件里：
        combined.sh  ->  "#!/bin/bash\nsource payload.sh"
        payload.sh   ->  "rm -rf /tmp/target"
    只解析第一层的话，combined.sh 正文完全无害，检查直接穿过去。

    逐行复用 script_targets：它已能区分「执行」与「读取」（`cat payload.sh`
    不产生引用），不必再写一套解析，两套解析必然慢慢分叉。
    注释行跳过——注释里的 `source x` 不会被执行。
    """
    out: list[str] = []
    for line in str(body or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.extend(script_targets(line))
    return out
