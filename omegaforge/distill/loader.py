"""OmegaForge SourceAgentLoader — eat any agent, any format.

Supported sources (auto-detected):
  * agency-swarm/agency-swarm style python classes   (.py with Agent impl)
  * langchain / langgraph definitions                (.py / .yaml / .json)
  * crewai Crew/Agent yaml                           (.yaml / .py)
  * autogen AssistantAgent configs                   (.json / .py)
  * raw system-prompt text                           (.txt / .md / paste)
  * any repo directory                               (scans & extracts)

The loader NEVER calls an LLM. It only performs deterministic extraction
of candidate signals （名称、角色线索、提示词文本、工具定义、工作流
cues). Semantic interpretation happens later in DistillEngine — this
separation keeps the ingest auditable and token-free.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.errors import UserError

# 单文件 / 目录扫描的体积上限。
# 现实仓库常混进 package-lock.json、数据 dump 这类数 MB～数十 MB 的文件，
# 原实现无上限整读：目录里一个 20MB 文件使进程 RSS 从 63MB 涨到 129MB。
MAX_FILE_BYTES = 8 * 1024 * 1024      # 单文件 8MB，超过则截断/跳过
MAX_TOTAL_BYTES = 32 * 1024 * 1024    # 目录累计 32MB
MAX_FILES = 2000                      # 最多扫 2000 个文件


def _read_capped(path: str) -> str:
    """读取文件，超过 MAX_FILE_BYTES 只取前一段。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(MAX_FILE_BYTES) if size > MAX_FILE_BYTES else f.read()


@dataclass
class SourceSignals:
    source_type: str                     # python|yaml|json|text|directory|unknown
    raw_text: str
    fingerprint: str                     # sha1 of raw
    name_hint: Optional[str] = None
    role_hints: list[str] = field(default_factory=list)
    prompt_candidates: list[str] = field(default_factory=list)
    tool_candidates: list[str] = field(default_factory=list)
    workflow_cues: list[str] = field(default_factory=list)
    file_list: list[str] = field(default_factory=list)

    def summary(self) -> str:
        # 这段文本会写进报告（engine.py:297 raw_summary）和运行记录
        # （engine.py:465 source_signals），是用户可见内容。
        # 原有写法是 dataclass 风格的 <source ... name=None>，
        # `name=None` 属于典型开发痕迹。
        type_cn = {"python": "Python 源码", "yaml": "YAML 配置",
                   "json": "JSON 配置", "text": "纯文本",
                   "directory": "项目目录", "unknown": "未识别格式"}.get(
                       self.source_type, "未识别格式")
        name = self.name_hint or "未识别到名称"
        return (f"{type_cn}，共 {len(self.raw_text)} 字符；"
                f"识别到 {len(self.prompt_candidates)} 段提示词候选、"
                f"{len(self.tool_candidates)} 个工具线索、"
                f"{len(self.workflow_cues)} 条工作流线索；名称：{name}")


TOOL_PATTERNS = [
    re.compile(r"""(?:def|function)\s+(?:([a-zA-Z_][\w]*)\s*\([^)]*\))
                (?:\s*(?:->|:)\s*[^\n]*description\s*[:=]\s*["']([^"']+)["'])?""",
               re.X),
    re.compile(r"""name\s*["']?\s*[:=]\s*["']([a-z_][\w_]*)["'][^{}]{0,200}?
                description\s*["']?\s*[:=]\s*["']([^"']+)["']""", re.X | re.S),
    re.compile(r"tools\s*[\"']?\s*[:=]\s*\[([^\]]{0,400})\]", re.S),
    re.compile(r"[-*]\s*([a-z_][\w_]*)\s*\(([^)]{0,80})\)\s*[:\-]\s*([^\n]{0,120})", re.M),
]

_TQ = '"' * 3

PROMPT_PATTERNS = [
    re.compile(
        r"(?:system_prompt|instructions|role_prompt|backstory|goal"
        r"|persona|system_message|SYSTEM|DESCRIPTION)\s*[\"']?\s*[:=]\s*("
        + _TQ + r".*?" + _TQ
        + r"|'''.*?'''"
        + r"|\"[^\"\n]{20,}\""
        + r"|'[^'\n]{20,}')",
        re.X | re.S),
    re.compile(r"^#{1,3}\s*(System Prompt|Persona|Role|Mission)\s*$\n(.+?)(?=^#|\Z)",
               re.M | re.S),
]

WORKFLOW_CUES = [
    "step", "then", "first", "finally", "pipeline", "workflow", "phase",
    "search", "analyze", "review", "plan", "execute", "verify", "report",
]

# 纯文本 提示词 识别。
# 现实中最常见的用法是"把一段 系统提示词 直接粘进来"，
# 但 PROMPT_PATTERNS 只认 system_prompt= / backstory: / # Persona 这类
# 结构化标记，导致粘贴的散文 提示词 一条都提取不到 —— 提示词_candidates
# 为空，后续对照组只能降级为 简化对照，"可验证更强"随之失效。
# 这里补一层语义判据：像指令（第二人称）且不像代码，就认定它是 提示词。
PROMPT_LIKE = re.compile(
    r"(you are|your (?:mission|role|task|goal|purpose)|you must|you should"
    r"|your job is|act as|你是一个|你是|你的(?:任务|角色|使命|目标)"
    r"|请(?:你)?|务必|必须|应该)",
    re.I)

CODE_LIKE = re.compile(
    r"^\s*(?:import\s+\w|from\s+[\w.]+\s+import|def\s+\w+|class\s+\w+"
    r"|#include|function\s+\w+|const\s+\w+\s*=|let\s+\w+\s*=|var\s+\w+\s*="
    r"|@\w+|<\?php|package\s+\w+)",
    re.M)


CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _info_len(t: str) -> int:
    """按"信息量"而非字符数计量长度。

    中文一字一词，英文要 5～6 个字符才构成一个词。原有写法统一用
    `len(t) < 40` 判断，等于按英文定尺子：例如 33 字通顺的中文指令
    被拒，而同义的 58 字符英文通过。
    """
    # 只取前 4000 字符估算：目的是判断"够不够一段指令"，
    # 不需要扫完整篇文档——在 3MB 文本上 findall 会物化出数十万
    # 个词，让进程内存多涨约 50MB。
    head = t[:4000]
    return (sum(1 for _ in CJK.finditer(head))
            + sum(1 for _ in LATIN_WORD.finditer(head)))


def looks_like_prompt(text: str) -> bool:
    """判断一段文本是否本身就是一个 系统提示词。

    代码优先排除：粘贴 python/js 源码时不该被当成提示词。
    """
    t = (text or "").strip()
    if _info_len(t) < 6:
        return False
    # 明显的代码特征（出现 2 次以上）→ 不是 提示词
    # 改用 finditer + 提前退出，避免在长文本上物化全量匹配列表
    n_code = 0
    for _ in CODE_LIKE.finditer(t):
        n_code += 1
        if n_code >= 2:
            return False
    return bool(PROMPT_LIKE.search(t))


class SourceAgentLoader:
    """Universal agent ingestion. `load(path_or_text)` returns signals."""

    def load(self, path_or_text: str) -> SourceSignals:
        # 类型检查：None/int/list 会让 os.path.exists 抛 TypeError，
        # 而 TypeError 在 errors.py 里被归类为 CODE_INTERNAL，
        # 用户看到"操作失败，请稍后重试"——把参数类型问题伪装成线上故障。
        if not isinstance(path_or_text, str):
            raise UserError("请提供有效的源材料：可以是提示词文本，或文件路径")
        if os.path.exists(path_or_text):
            if os.path.isdir(path_or_text):
                return self._load_directory(path_or_text)
            return self._load_file(path_or_text)
        body = path_or_text.strip()
        if not body:
            raise UserError("源材料为空，请粘贴提示词或指定文件路径")
        # 旧门槛是"字符数 > 40"，按英文定尺子：19～40 字的中文指令被误拒。
        # 但不能简单收紧成"必须读起来像指令"——源码 / YAML / JSON 这类源材料
        # 本来就没有指令措辞，那样会把它们一起拒掉（例如 THIN_SOURCE 直接崩，
        # 而它正是用来验证"无提示词时应降级为不可比"的样本）。
        # 因此取并集：够长（兼容旧行为）**或**本身像一段指令。
        if len(body) > 40 or looks_like_prompt(body):
            return self._load_text(body, source_type="text")
        # 原有写法抛 FileNotFoundError，经 errors.py 映射后显示
        # "未找到对应记录"——把"内容不足"说成"文件/记录不存在"，方向完全错。
        raise UserError(
            "这段内容太短或缺少指令，无法识别为有效的 Agent 提示词，"
            "请补充完整后重试")

    # ------------------------------------------------------------------
    def _load_file(self, path: str) -> SourceSignals:
        ext = os.path.splitext(path)[1].lower()
        raw = _read_capped(path)
        st = {".py": "python", ".yaml": "yaml", ".yml": "yaml",
              ".json": "json", ".md": "text", ".txt": "text",
              ".toml": "yaml"}.get(ext, "unknown")
        return self._extract(raw, st, [os.path.basename(path)],
                             name_hint=os.path.splitext(
                                 os.path.basename(path))[0])

    def _load_text(self, text: str, source_type: str = "text") -> SourceSignals:
        return self._extract(text, source_type, ["<pasted>"])

    def _load_directory(self, root: str) -> SourceSignals:
        chunks, files = [], []
        total = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", "node_modules", "__pycache__",
                                        ".venv", "dist", "build"}]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in {
                        ".py", ".yaml", ".yml", ".json", ".md", ".txt"}:
                    continue
                if len(files) >= MAX_FILES:
                    break
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root)
                try:
                    # 超大文件（lock / 数据 dump）跳过，避免整进程内存翻数倍
                    if os.path.getsize(p) > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        body = f.read()
                except OSError:
                    continue
                if total + len(body) > MAX_TOTAL_BYTES:
                    break
                chunks.append(f"\n===== FILE: {rel} =====\n" + body)
                files.append(rel)
                total += len(body)
        raw = "".join(chunks)
        return self._extract(raw, "directory", files,
                             name_hint=os.path.basename(root.rstrip("/")))

    # ------------------------------------------------------------------
    def _extract(self, raw: str, source_type: str, file_list: list[str],
                 name_hint: Optional[str] = None) -> SourceSignals:
        sig = SourceSignals(
            source_type=source_type, raw_text=raw, file_list=file_list,
            fingerprint=hashlib.sha1(raw.encode()).hexdigest()[:12],
            name_hint=name_hint)

        # name hints from code
        if not sig.name_hint or sig.name_hint == "<pasted>":
            # 自称句式优先：源 Agent 最常见的开场就是 "You are X, ..."。
            # 只认紧跟的大写开头标识符，因此 "You are a meticulous
            # research assistant" 这类描述句不会命中（冠词后是小写形容词）。
            m0 = re.search(
                r"""(?i:you are|you're|你是)\s+(?:a|an|the)?\s*"""
                r"""([A-Z][A-Za-z0-9]{2,30})""", raw)
            m = re.search(r"""(?:class|name)\s+([A-Z][\w]+(?:Agent|Assistant)?)""",
                          raw)
            m2 = re.search(r"""name\s*[:=]\s*["']([\w\- ]{3,40})["']""", raw)
            sig.name_hint = (m0.group(1) if m0 else
                             (m.group(1) if m else
                              (m2.group(1) if m2 else None)))

        # role hints
        for kw in ("research", "analyst", "writer", "reviewer", "planner",
                   "coder", "executor", "critic", "manager", "assistant",
                   "tutor", "translator", "summarizer", "scraper"):
            if kw in raw.lower():
                sig.role_hints.append(kw)

        # 提示词候选 —— 取 the longest captured group (body over heading)
        for pat in PROMPT_PATTERNS:
            for m in pat.finditer(raw):
                groups = [g for g in m.groups() if g]
                if not groups:
                    continue
                text = max(groups, key=len).strip()
                text = re.sub(r"^[\"']{1,3}", "", text)
                text = re.sub(r"[\"']{1,3}$", "", text).strip()
                if len(text) > 15:
                    cand = text[:4000]
                    # 同一段提示词常被多个模式同时命中，不去重会产生
                    # 完全相同的重复项（例如 2 条），既费 token 又虚增计数
                    if cand not in sig.prompt_candidates:
                        sig.prompt_candidates.append(cand)

        # tool candidates
        for pat in TOOL_PATTERNS:
            for m in pat.finditer(raw):
                parts = [g for g in m.groups() if g]
                if parts:
                    sig.tool_candidates.append(
                        " | ".join(p.strip() for p in parts)[:200])

        # workflow cues
        low = raw.lower()
        for cue in WORKFLOW_CUES:
            if cue in low:
                sig.workflow_cues.append(cue)

        # markdown/text: first H1 heading is the most faithful name
        if source_type == "text":
            mh = re.search(r"^#\s+(.{3,60})\s*$", raw, re.M)
            if mh:
                sig.name_hint = mh.group(1).strip()

        # 兜底：结构化标记一条都没命中，但整段读起来就是一段指令
        # → 它本身就是 提示词（用户最常见的使用方式）
        if not sig.prompt_candidates and looks_like_prompt(raw):
            sig.prompt_candidates.append(raw.strip()[:4000])

        return sig
