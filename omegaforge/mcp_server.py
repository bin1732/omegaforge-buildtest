"""OmegaForge MCP Server — 把 OmegaForge 暴露为标准 MCP 服务器。

协议：JSON-RPC 2.0 over stdio（newline-delimited），兼容 MCP 规范
（initialize / tools/list / tools/call / ping）。任何 MCP 客户端
（Claude Desktop、Cursor、其他 agent 框架）都能挂载。

启动：OMEGAFORGE_PROVIDER=zai python3 -m omegaforge.mcp_server
注册的工具：
  omegaforge_distill   蒸馏一个源 agent
  omegaforge_run       运行已蒸馏的 genome
  kb_add / kb_search   知识库写入 / 检索
  memory_remember / memory_recall   长期记忆
  wiki_save / wiki_get / wiki_search Wikipedia 式页面
  task_add / task_list / task_done  待办清单
  skill_invoke         技能调用
"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional
import threading

from .llm.client import LLMClient
from .core.budget import TokenBank
from .core.limits import (MIN_BUDGET, MAX_BUDGET, DEFAULT_BUDGET,
                          ROUNDS_MIN, ROUNDS_MAX, ROUNDS_DEFAULT,
                          GENS_MIN, GENS_MAX, GENS_DEFAULT,
                          SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX,
                          SEARCH_LIMIT_DEFAULT,
                          PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT)
from .core.validate import as_int
from .distill.engine import DistillEngine
from .agent.super_agent import SuperAgent
from .memory.kb import KnowledgeBase
from .memory.wiki import Wiki
from .memory.tasks import Tasks
from .skills.manager import SkillManager
from .tools.system_tools import tool_dispatch as sys_tool_dispatch
from .tools.system_tools import (MCP_SCOPE_DEFAULTS, McpScope, Permissions,
                                 McpScopeDenied)
from .tools.policy import (PERSONAL_WRITE_TOOLS, audit_write,
                           gate_personal_write)
from .tools.policy import TOOL_RISK

# MCP 对外工具名 → 门禁内部名。两者不同（对外 run_command，内部 terminal），
# 作用域判定必须用内部名，否则查表查不到，等于这条限制从未生效。
_SYS_DISPATCH = {"run_command": "terminal", "fs_read": "fs.read",
                 "fs_write": "fs.write", "fs_list": "fs.list",
                 "web_fetch": "web_fetch"}


def _sys_available(mcp_name: str) -> bool:
    """该 MCP 系统工具是否真的可用：本机能力开关 AND MCP 作用域。"""
    internal = _SYS_DISPATCH.get(mcp_name)
    if not internal:
        # 个人数据写工具：登记在作用域表里，但名字与内部名一致（不经过
        # 内部名映射）。这里必须单独判定，否则作用域对它们不会生效。
        if mcp_name in MCP_SCOPE_DEFAULTS:
            return bool(McpScope().load().get(mcp_name))
        return True
    cap = TOOL_RISK.get(internal, ("", "high"))[0]
    if cap and not Permissions().load().get(cap):
        return False
    if internal in MCP_SCOPE_DEFAULTS and not McpScope().load().get(internal):
        return False
    return True


def _model_safe(out):
    """工具结果出网前收口：喂模型的一律用带分隔标记的包裹版。

    为什么必须在这里收口
    --------------------
    `fs_read` / `web_fetch` 返回里同时有 text（原文）和 text_wrapped
    （带来源分隔标记）。分隔标记只有被真正送进上下文才有意义——而 MCP 这条
    通道的消费方**一定是外部模型**，不是人。出口不能直接输出整个结果：
    若给出的是未经处理的原文，分隔标记就白做了——网页里那句"忽略以上所有
    指令"会原样进入别人的模型，与用户指令在结构上完全同构。

    本机 HTTP 出口不这么做，因为那一侧消费方是界面（人读），塞分隔标记
    会让"读个文件"变成一大段前缀。不同出口、不同约定——与
    "能力发现 ≠ 执行权限"是同一条原则。

    只替换不新增字段：text_wrapped 撤掉避免重复，suspicious /
    injection_tags 等元数据保留（中文标签，对模型与审计都有用）。
    """
    if not isinstance(out, dict):
        return out
    wrapped = out.get("text_wrapped")
    if not isinstance(wrapped, str) or not wrapped:
        return out
    safe = dict(out)
    safe["text"] = wrapped
    safe.pop("text_wrapped", None)
    return safe

from .core.errors import configure_logging, user_error, UserError

SERVER_INFO = {"name": "omegaforge", "version": "0.2.0"}
PROTOCOL_VERSION = "2024-11-05"
# 版本协商：客户端可能请求更高版本。服务器只回自己真正实现的版本，
# 由客户端决定接受还是断开——硬编码回一个版本虽能连通，但会让
# "客户端以为拿到了 2025 特性" 变成静默降级，故显式列出并协商。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC / MCP 错误码（协议层，区别于工具执行失败）
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602

# 参数类型 → 中文名，用于给"调用方"（人或模型）可操作的提示
_CN_TYPE = {"string": "文本", "integer": "整数", "number": "数字",
            "array": "列表", "object": "对象", "boolean": "是/否"}

#: 参数名 → 面向调用方的中文名。
#:
#: 为什么必须有这张表：协议里的参数名是内部标识（cmd / slug / task_id ...），
#: 直接回显会让调用方看到「缺少必填参数：cmd」——调用方无从知道要去哪里补。
#: 更糟的是 `参数「text」的格式不正确，应为文本` 这种自相矛盾的句子。
#:
#: 同一个参数名在不同工具里含义不同（text 在 kb_add 是正文、在 task_add
#: 是待办内容；path 在 fs_read 是文件、在 fs_list 是目录），所以按
#:「工具 + 参数」配对登记，查不到时退回通用表。
_CN_PARAM: dict[str, dict[str, str]] = {
    "omegaforge_distill": {"source": "源材料", "budget": "预算",
                           "rounds": "轮次", "gens": "进化代数",
                           "ablate": "是否做归因", "eval_set": "评测集",
                           "baseline_prompt": "对照组提示词"},
    "omegaforge_compare": {"prev": "上一版本结果", "curr": "当前结果"},
    "omegaforge_run":     {"genome_path": "产物路径", "task": "任务内容"},
    "kb_add":             {"title": "标题", "text": "正文",
                           "type": "类型", "tags": "标签"},
    "kb_search":          {"query": "关键词", "limit": "条数上限"},
    "memory_remember":    {"fact": "要记住的内容", "tags": "标签"},
    "memory_recall":      {"query": "关键词"},
    "wiki_save":          {"slug": "词条标识", "title": "标题",
                           "body": "正文"},
    "wiki_get":           {"slug": "词条标识"},
    "wiki_search":        {"query": "关键词"},
    "task_add":           {"text": "待办内容", "priority": "优先级"},
    "task_list":          {"scope": "范围"},
    "task_done":          {"task_id": "待办编号"},
    "skill_invoke":       {"name": "技能名称", "context": "上下文"},
    "run_command":        {"cmd": "命令", "timeout": "超时秒数"},
    "fs_read":            {"path": "文件路径"},
    "fs_write":           {"path": "文件路径", "content": "文件内容"},
    "fs_list":            {"path": "目录路径"},
    "web_fetch":          {"url": "网址"},
}

#: 通用兜底：只在按工具查不到时用，避免新参数漏登记时回显英文标识。
_CN_PARAM_ANY = {"query": "关键词", "path": "路径", "text": "内容",
                 "name": "名称", "title": "标题", "limit": "条数上限",
                 "tags": "标签", "type": "类型", "url": "网址",
                 "content": "内容", "body": "正文", "context": "上下文"}


def _cn_param(tool: str, key: str) -> str:
    """参数名的中文说法（按工具精确匹配，其次通用表，最后回退原名）。"""
    return (_CN_PARAM.get(tool, {}).get(key)
            or _CN_PARAM_ANY.get(key)
            or key)


def _run_dir(source: str) -> str:
    """把外部可控的 source 折成一个安全的单层目录名。

    例如：只把 "/" 换成 "_"，于是 ".." 原样保留——
    source=".." 会让产物写到 mcp_runs 的**父目录**（即用户数据根目录），
    覆盖掉与本次蒸馏无关的文件。反斜杠也没处理。
    """
    safe = "".join(c if c.isalnum() or c in "._-" else "_"
                   for c in str(source))[:40].strip("._ ")
    return safe or "run"


class McpCore:
    """协议无关的工具实现（可独立测试）。"""

    def __init__(self):
        self.kb = KnowledgeBase()
        self.wiki = Wiki(kb=self.kb)
        self.tasks = Tasks()
        self.skills = SkillManager()
        self.llm = LLMClient()

    # -- 入参校验（schema 驱动）------------------------------------------
    @classmethod
    def _type_ok(cls, value, declared: str) -> bool:
        if declared == "string":
            return isinstance(value, str)
        if declared == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if declared == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if declared == "array":
            return isinstance(value, list)
        if declared == "object":
            return isinstance(value, dict)
        if declared == "boolean":
            return isinstance(value, bool)
        return True

    @classmethod
    def _validate(cls, name: str, args: dict) -> None:
        """按 inputSchema 校验必填与类型。

        为什么放在这里：schema 里已经声明了 required，但 call_tool 各分支
        仍用 args["x"] 硬取，缺参数就是 KeyError → 被转译层归为内部错误，
        调用方（人或模型）只看到"操作失败，请稍后重试"，无从纠正。
        统一在这里校验，schema 与校验共用一份声明，不会两边走偏。
        """
        spec = next((t for t in cls.TOOLS if t["name"] == name), None)
        if spec is None:
            raise UserError(f"未找到名为「{name}」的工具")
        schema = spec.get("inputSchema") or {}
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        required = (schema.get("required") or []) if isinstance(schema, dict) else []

        missing = []
        for key in required:
            if key not in args:
                missing.append(_cn_param(name, key))
            elif args[key] is None or args[key] == "":
                missing.append(_cn_param(name, key))
        if missing:
            raise UserError("还缺少必填内容：" + "、".join(missing))

        for key, value in (args or {}).items():
            declared = (props.get(key) or {}).get("type")
            if declared and not cls._type_ok(value, declared):
                raise UserError(
                    f"「{_cn_param(name, key)}」的格式不正确"
                    f"（应填写{_CN_TYPE.get(declared, declared)}）")

    # -- tool dispatch ---------------------------------------------------
    @classmethod
    def _public_tools(cls) -> list:
        """tools/list 的输出：schema 原样透传，叠加 annotations。

        不改 cls.TOOLS 本身——_validate 依赖它做必填校验，若把 annotations
        混进同一份结构，schema 与校验的边界就糊了。

        按当前可用性过滤：若无条件列出全部 19 个工具，而系统工具的
        capability 默认关闭，调用必然失败并返回"请在设置→系统能力中开启"。
        MCP host 是另一个进程，它背后的模型既打不开本 app 的设置界面，
        也不知道该怎么开——只会反复重试或换条路绕。列出来而永远不可用，
        比不列更糟：那是一个承诺，也是一个诱导。
        """
        out = []
        for spec in cls.TOOLS:
            if not _sys_available(spec["name"]):
                continue
            item = dict(spec)
            ann = cls.TOOL_ANNOTATIONS.get(spec["name"])
            if ann:
                item["annotations"] = ann
            out.append(item)
        return out

    def call_tool(self, name: str, args: dict) -> dict:
        args = self._normalize_args(name, args or {})
        self._validate(name, args)
        # 作用域必须在这里**真的拦**，不能只靠 tools/list 不列：
        # 外部模型可能从别处（提示词、文档、上一次会话）知道工具名，
        # 直接 tools/call 打过来。只过滤列表等于"没写进说明书就不存在"，
        # 那是把访问控制当成了 UI 装饰。
        # 只在这里拦**不走 tool_dispatch** 的个人数据写工具。系统工具
        # （run_command / fs_write / web_fetch…）必须交给 sys_tool_dispatch：
        # 那里 critical 检查排在作用域之前，先拦具体原因。在此处统一预审
        # 会把"169.254.169.254 属于内网地址"压成一句通用的"未授权"，
        # 具体、可行动的原因就丢了。
        if (name in MCP_SCOPE_DEFAULTS and name not in _SYS_DISPATCH
                and not McpScope().load().get(name)):
            try:
                audit_write({"tool": name, "origin": "mcp",
                             "verdict": "deny", "reason": "mcp_scope_off"})
            except Exception:                    # noqa: BLE001
                pass
            raise McpScopeDenied(name)
        if name in PERSONAL_WRITE_TOOLS:
            # 作用域开过之后，还要再过一次**四级权限矩阵**。
            #
            # kb_add 走了作用域就直接 self.kb.add() 写入，
            # 从不咨询 policy。于是同一个 MCP 客户端、同一个最保守档
            # （confirm）：
            #     fs_write → 「该操作需要你确认后才会执行」（拒绝）
            #     kb_add   → 写入成功，且审计里一条都没有
            # 两者都是"写用户数据"，却只有一半受矩阵约束。而 TOOL_RISK 里
            # 明明写着 kb_add 是 medium（confirm 下应为 ask）——分级只落在
            # 表里，没落在这条唯一真正对外的数据写入路径上。
            #
            # 与 fs.write 同标：ask → ApprovalRequired，plan → PlanRequired，
            # 都由 _classify 转成可展示文案，绝不静默放行。
            gate_personal_write(name, args, origin="mcp")
        if name == "omegaforge_distill":
            source = str(args.get("source", "")).strip()
            if not source:
                raise ValueError("source required")
            # 数值边界必须走 limits.py 这一唯一真源。
            # 这里写的是裸 int(args.get(...))，而 CLI 与
            # server 都用 as_int(..., minimum=, maximum=)。于是同一批输入
            # 在两个入口被拒、在 MCP 被放行——
            #   rounds=0 / gens=0   → 空转产物（0.00 分）却照样返回成功
            #   rounds=10**9        → 十亿轮评测
            #   budget=-1           → TokenBank 被压成 0
            # 而 MCP 是**唯一面向外部模型**的入口：调用方不是人，是别的
            # agent 按 schema 猜出来的值，越界是常态而非意外。
            budget = as_int(args, "budget", DEFAULT_BUDGET,
                            minimum=MIN_BUDGET, maximum=MAX_BUDGET,
                            label="预算")
            rounds = as_int(args, "rounds", ROUNDS_DEFAULT,
                            minimum=ROUNDS_MIN, maximum=ROUNDS_MAX,
                            label="评测轮数")
            gens = as_int(args, "gens", GENS_DEFAULT,
                          minimum=GENS_MIN, maximum=GENS_MAX,
                          label="进化代数")
            # 冻结评测集：只接受**内联用例**，不接受文件路径。
            # 接受路径等于让外部模型指定本机任意文件去读（HTTP 侧已按同一
            # 原则处理）。MCP 的调用方更不可信，这里更不能开口子。
            raw_eval = args.get("eval_set")
            eval_set = (DistillEngine._normalize_eval_set(raw_eval)
                        if raw_eval is not None else None)
            bank = TokenBank(budget)
            engine = DistillEngine(self.llm, bank, arena_rounds=rounds,
                                   max_generations=gens, verbose=False,
                                   eval_set=eval_set)
            out = os.path.join(self.kb.home, "mcp_runs", _run_dir(source))
            genome, report = engine.distill(
                source, output_dir=out,
                ablate=bool(args.get("ablate", False)),
                baseline_prompt=str(args.get("baseline_prompt", "") or ""))
            return {"verdict": report.verdict,
                    "distilled": round(report.final_score, 2),
                    "baseline": round(report.baseline_score, 2),
                    "tokens_spent": bank.total_spent,
                    "genome_path": os.path.join(out, "genome.json"),
                    "name": genome.name,
                    # 比对所需字段必须随结果一起给出：外部模型手里只有这
                    # 一份返回，缺了指纹或结论成立标记就无法判断"本版比
                    # 上一版本强"，只能拿两个裸分数硬比——而不同题的分数
                    # 相减是没有意义的数字。
                    "eval_set_fingerprint": report.eval_set_fingerprint,
                    "eval_set_source": report.eval_set_source,
                    "eval_set_cases": report.eval_set_cases,
                    "baseline_comparable": report.baseline_comparable,
                    "claim_valid": report.claim_valid,
                    "self_certified": report.self_certified,
                    "generation": report.generation,
                    "generations_run": report.generations_run,
                    "rolled_back": report.rolled_back,
                    "ablation": getattr(report, "ablation", None) or {}}
        if name == "omegaforge_compare":
            # 只吃两份**内联**报告对象，不碰文件系统：一旦接受路径就等于
            # 让外部模型指定本机任意文件去读。外部模型手里本就有每次
            # distill 的返回（含指纹与 结论是否成立），直接回传即可。
            return DistillEngine.compare_reports(args.get("prev") or {},
                                                 args.get("curr") or {})
        if name == "omegaforge_run":
            genome = SuperAgent.from_genome_file(
                str(args["genome_path"]), self.llm, TokenBank(100000))
            res = genome.run(str(args["task"]))
            return {"ok": res.ok, "answer": res.answer[:2000],
                    "tokens": res.tokens_used, "steps": res.steps_executed}
        if name == "kb_add":
            doc_id = self.kb.add(str(args["title"]), str(args.get("text", "")),
                                 type=str(args.get("type", "note")),
                                 tags=args.get("tags"))
            return {"id": doc_id, "total": self.kb.count()}
        if name == "kb_search":
            return {"results": self.kb.search(
                str(args.get("query", "")),
                # 直接用 int() 不够：schema 只校验了"是不是整数"，没校验范围，
                # 于是 limit=-1 / 10**9 一路放行（只有 kb 内部的 _safe_limit
                # 兜住才没出事）。三个入口必须同一口径——distill 的参数
                # 已经用 as_int(..., minimum=, maximum=)，这里补上。
                limit=as_int(args, "limit", SEARCH_LIMIT_DEFAULT,
                             minimum=SEARCH_LIMIT_MIN,
                             maximum=SEARCH_LIMIT_MAX, label="返回条数"))}
        if name == "memory_remember":
            return {"id": self.kb.remember(str(args["fact"]),
                                           tags=args.get("tags"))}
        if name == "memory_recall":
            return {"memories": self.kb.recall(str(args.get("query", "")))}
        if name == "wiki_save":
            slug = self.wiki.save(str(args["slug"]), str(args["title"]),
                                  str(args.get("body", "")))
            return {"slug": slug, "backlinks": self.wiki.backlinks(slug)}
        if name == "wiki_get":
            return self.wiki.get(str(args["slug"])) or {"error": "not found"}
        if name == "wiki_search":
            return {"results": self.wiki.search(str(args.get("query", "")))}
        if name == "task_add":
            return self.tasks.add(str(args["text"]),
                                  as_int(args, "priority", PRIORITY_DEFAULT,
                                         minimum=PRIORITY_MIN,
                                         maximum=PRIORITY_MAX,
                                         label="优先级"))
        if name == "task_list":
            return {"items": self.tasks.list(str(args.get("scope", "pending"))),
                    "stats": self.tasks.stats()}
        if name == "task_done":
            return {"completed": self.tasks.complete(str(args["task_id"]))}
        if name == "skill_invoke":
            return {"prompt": self.skills.invoke(str(args["name"]),
                                                 str(args.get("context", "")))}
        if name == "skill_list":
            return {"skills": self.skills.list()}
        if name in ("run_command", "fs_read", "fs_write", "fs_list",
                    "web_fetch"):
            return sys_tool_dispatch(name, args, origin="mcp")
        raise UserError(f"未找到名为「{name}」的工具")

    # -- schemas -----------------------------------------------------------
    TOOLS = [
        {"name": "omegaforge_distill",
         "description": "把一段 Agent 源材料提炼为能力更强的产物。"
                        "参数：source（源材料，文件路径或文本）、"
                        "budget（预算）、rounds（轮次）、gens（进化代数）、"
                        "eval_set（评测集，需以内联用例提供，不接受文件路径）、"
                        "ablate（是否做归因分析）、"
                        "baseline_prompt（对照组提示词；源材料中无法提取提示词"
                        "时提供它，结论才具备可比性）",
         "inputSchema": {"type": "object", "properties": {
             "source": {"type": "string"}, "budget": {"type": "integer"},
             "rounds": {"type": "integer"}, "gens": {"type": "integer"},
             "ablate": {"type": "boolean"},
             "eval_set": {"type": "array",
                          "description": "inline [{id,input,rubric}]"},
             "baseline_prompt": {"type": "string",
                                 "description": "baseline agent system "
                                 "prompt (inline text, not a path)"}},
             "required": ["source"]}},
        {"name": "omegaforge_compare",
         "description": "比对两次提炼的结果（各为 omegaforge_distill 的返回）。"
                        "两次若使用了不同的评测集，将拒绝给出差值。",
         "inputSchema": {"type": "object", "properties": {
             "prev": {"type": "object"}, "curr": {"type": "object"}},
             "required": ["prev", "curr"]}},
        {"name": "omegaforge_run",
         "description": "用提炼产物执行一项任务。"
                        "参数：genome_path（产物路径）、task（任务内容）",
         "inputSchema": {"type": "object", "properties": {
             "genome_path": {"type": "string"}, "task": {"type": "string"}},
             "required": ["genome_path", "task"]}},
        {"name": "kb_add",
         "description": "向知识库添加一篇文档。"
                        "参数：title（标题）、text（正文）、type（类型）、tags（标签）",
         "inputSchema": {"type": "object", "properties": {
             "title": {"type": "string"}, "text": {"type": "string"},
             "type": {"type": "string"}, "tags": {"type": "array"}},
             "required": ["title", "text"]}},
        {"name": "kb_search",
         "description": "检索知识库。参数：query（关键词）、limit（条数上限）",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"}, "limit": {"type": "integer"}},
             "required": ["query"]}},
        {"name": "memory_remember",
         "description": "记住一条长期记忆。参数：fact（要记住的内容）、tags（标签）",
         "inputSchema": {"type": "object", "properties": {
             "fact": {"type": "string"}, "tags": {"type": "array"}},
             "required": ["fact"]}},
        {"name": "memory_recall",
         "description": "按关键词回忆长期记忆。参数：query（关键词）",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"}}, "required": ["query"]}},
        {"name": "wiki_save",
         "description": "新建或更新一篇词条（Markdown 正文）。"
                        "参数：slug（词条标识）、title（标题）、body（正文）",
         "inputSchema": {"type": "object", "properties": {
             "slug": {"type": "string"}, "title": {"type": "string"},
             "body": {"type": "string"}}, "required": ["slug", "title"]}},
        {"name": "wiki_get",
         "description": "读取一篇词条及其反向链接。参数：slug（词条标识）",
         "inputSchema": {"type": "object", "properties": {
             "slug": {"type": "string"}}, "required": ["slug"]}},
        {"name": "wiki_search",
         "description": "搜索词条。参数：query（关键词）",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"}}, "required": ["query"]}},
        {"name": "task_add",
         "description": "新增一条待办。参数：text（待办内容）、priority（优先级，1 到 3）",
         "inputSchema": {"type": "object", "properties": {
             "text": {"type": "string"}, "priority": {"type": "integer"}},
             "required": ["text"]}},
        {"name": "task_list",
         "description": "列出待办。参数：scope（范围，可选未完成、已完成或全部）",
         "inputSchema": {"type": "object", "properties": {
             "scope": {"type": "string"}}, "required": []}},
        {"name": "task_done",
         "description": "把一条待办标记为完成。参数：task_id（待办编号）",
         "inputSchema": {"type": "object", "properties": {
             "task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "skill_list",
         "description": "列出已安装的技能（仅含名称与说明）。",
         "inputSchema": {"type": "object", "properties": {}, "required": []}},
        {"name": "skill_invoke",
         "description": "调用一个已安装的技能，返回其提示内容。"
                        "参数：name（技能名称）、context（上下文）",
         "inputSchema": {"type": "object", "properties": {
             "name": {"type": "string"}, "context": {"type": "string"}},
             "required": ["name"]}},
        {"name": "run_command",
         "description": "在本机执行一条终端命令（需先开启终端能力，"
                        "单次最长 60 秒，操作会被记录）。"
                        "参数：cmd（命令）、timeout（超时秒数）",
         "inputSchema": {"type": "object", "properties": {
             "cmd": {"type": "string"}, "timeout": {"type": "integer"}},
             "required": ["cmd"]}},
        {"name": "fs_read",
         "description": "读取工作区内的一个文件。参数：path（文件路径）",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string"}}, "required": ["path"]}},
        {"name": "fs_write",
         "description": "写入工作区内的一个文件。参数：path（文件路径）、content（文件内容）",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string"}, "content": {"type": "string"}},
             "required": ["path", "content"]}},
        {"name": "fs_list",
         "description": "列出工作区内的文件。参数：path（目录路径）",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string"}}, "required": []}},
        {"name": "web_fetch",
         "description": "抓取一个网页并返回提取出的正文。参数：url（网址）",
         "inputSchema": {"type": "object", "properties": {
             "url": {"type": "string"}}, "required": ["url"]}},
    ]

    # 工具语义标注（MCP 2025-06-18 起支持 annotations）。
    # 为什么必须加：MCP 的 host（Claude Desktop / Cursor）靠这几个 hint 决定
    # "要不要弹确认、弹多大"。我们内部已有 TOOL_RISK 四级门禁，但那份风险
    # 分级只活在自己进程里——外部 host 看不到，它就只能把所有工具一视同仁，
    # 于是 run_command 和 kb_search 在用户面前长得一模一样。
    # 这里把内部风险分级翻译成协议通用语，让门禁边界跨进程生效。
    _READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
                  "idempotentHint": True, "openWorldHint": False}
    _WRITE_LOCAL = {"readOnlyHint": False, "destructiveHint": True,
                    "idempotentHint": True, "openWorldHint": False}
    _EXEC = {"readOnlyHint": False, "destructiveHint": True,
             "idempotentHint": False, "openWorldHint": True}
    _NET_READ = {"readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True}

    TOOL_ANNOTATIONS: dict[str, dict] = {
        "omegaforge_distill": {"title": "蒸馏 Agent", **_WRITE_LOCAL,
                               "idempotentHint": False},
        "omegaforge_compare": {"title": "比对两版蒸馏结果", **_READ_ONLY},
        "omegaforge_run":     {"title": "运行提炼产物", **_EXEC,
                               "destructiveHint": False},
        "kb_add":             {"title": "写入知识库", **_WRITE_LOCAL,
                               "idempotentHint": False},
        "kb_search":          {"title": "检索知识库", **_READ_ONLY},
        "memory_remember":    {"title": "记住事实", **_WRITE_LOCAL,
                               "idempotentHint": False},
        "memory_recall":      {"title": "回忆记忆", **_READ_ONLY},
        "wiki_save":          {"title": "保存词条", **_WRITE_LOCAL},
        "wiki_get":           {"title": "读取词条", **_READ_ONLY},
        "wiki_search":        {"title": "搜索词条", **_READ_ONLY},
        "task_add":           {"title": "新增待办", **_WRITE_LOCAL,
                               "idempotentHint": False},
        "task_list":          {"title": "列出待办", **_READ_ONLY},
        "task_done":          {"title": "完成待办", **_WRITE_LOCAL},
        "skill_list":         {"title": "列出技能", **_READ_ONLY},
        "skill_invoke":       {"title": "调用技能", **_WRITE_LOCAL,
                               "idempotentHint": False},
        "run_command":        {"title": "执行终端命令", **_EXEC},
        "fs_read":            {"title": "读取文件", **_READ_ONLY},
        "fs_write":           {"title": "写入文件", **_WRITE_LOCAL},
        "fs_list":            {"title": "列出目录", **_READ_ONLY},
        "web_fetch":          {"title": "抓取网页", **_NET_READ},
    }

    # 参数别名（别名 -> 规范名）。
    # 为什么 MCP 层需要、内部层不需要：调用方是**别人的模型**，它只看到工具名
    # 和 schema，只能按语义猜参数名。工具叫 run_command，它大概率传 command；
    # 我们内部叫 cmd，结果每次调用都被"缺少必填参数：cmd"拒绝，而模型无从得知
    # 正确名字（错误文案里给的正是它没传的那个键，形成死循环）。
    # 内部前端/CLI 对不上可以改代码对齐；MCP 对不上是永久失效，且改不了客户端。
    ARG_ALIASES: dict[str, dict[str, str]] = {
        "run_command": {"command": "cmd"},
        "fs_write": {"text": "content"},
        "task_add": {"content": "text"},
        "task_done": {"id": "task_id"},
        "kb_add": {"content": "text"},
        "memory_remember": {"content": "fact", "text": "fact"},
        "omegaforge_run": {"genome": "genome_path"},
    }

    @classmethod
    def _normalize_args(cls, name: str, args: dict) -> dict:
        """把客户端可能用的同义参数名映射到本站规范名。

        只做「改名」不做「补全」：别名与规范名同时出现时以规范名为准，
        避免模型一次传两个、我们替它猜一个。
        """
        aliases = cls.ARG_ALIASES.get(name) or {}
        if not aliases:
            return args
        out = {}
        for key, value in args.items():
            canon = aliases.get(key, key)
            if canon in out and canon in args:
                continue          # 规范名优先，别名让位
            out[canon] = value
        if args:
            for canon in set(aliases.values()):
                if canon in args:
                    out[canon] = args[canon]
        return out

    # -- JSON-RPC dispatch ---------------------------------------------
    def handle(self, msg: dict) -> Optional[dict]:
        """处理单条 JSON-RPC 消息。

        三条硬约束（）：
          1. 消息必须是 dict。若 msg.get 写在 try 之外，客户端发一个
             "[]" / "null" / "123" 就会 AttributeError 冒泡出主循环，
             整个 MCP 服务进程直接退出（rc=1），此后所有请求全部无响应。
             一条畸形消息即永久打死服务，且客户端只表现为"工具全部失效"。
          2. 支持 batch（JSON 数组）。JSON-RPC 2.0 允许，且上面那个崩溃
             的根因正是没区分 dict 与 list，故一并处理而不是简单拒绝。
          3. 工具执行失败必须走 result + isError，而不是 JSON-RPC error。
             后者是协议级失败，客户端会当成连接问题；前者才是 MCP 规定的
             "工具跑挂了"，模型能看到原因并自我纠正（换参数/换工具）。
        """
        # ① 类型闸门：非 dict 一律 Invalid Request，绝不让它碰到 .get
        if not isinstance(msg, dict):
            return self._err(None, E_INVALID_REQUEST, "无效请求：消息必须是一个 JSON 对象")

        method = msg.get("method", "")
        mid = msg.get("id")
        try:
            if not isinstance(method, str) or not method:
                return self._err(mid, E_INVALID_REQUEST, "无效请求：缺少 method")

            if method == "initialize":
                params = msg.get("params") or {}
                wanted = params.get("protocolVersion") if isinstance(params, dict) else None
                # 客户端版本若在我们实现的范围内就用它，否则回落到我们最新的实现
                version = (wanted if wanted in SUPPORTED_PROTOCOL_VERSIONS
                           else PROTOCOL_VERSION)
                result = {"protocolVersion": version,
                          "capabilities": {"tools": {}},
                          "serverInfo": SERVER_INFO}
            elif method == "notifications/initialized":
                result = {}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._public_tools()}
            elif method == "tools/call":
                params = msg.get("params")
                if not isinstance(params, dict):
                    return self._err(mid, E_INVALID_PARAMS,
                                     "调用参数格式不正确，请填写一组参数")
                name = params.get("name")
                arguments = params.get("arguments")
                if not isinstance(name, str) or not name:
                    return self._err(mid, E_INVALID_PARAMS, "缺少工具名")
                if arguments is not None and not isinstance(arguments, dict):
                    return self._err(mid, E_INVALID_PARAMS,
                                     "工具参数格式不正确，请填写一组参数")
                try:
                    out = self.call_tool(name, arguments or {})
                    out = _model_safe(out)
                except Exception as e:                          # noqa: BLE001
                    # ③ 工具级失败 → isError，模型可读、可自我纠正。
                    # 异常原文可能带接口地址与密钥片段；MCP 客户端日志常外传，
                    # 因此这里同样只给可展示文案，完整异常进内部日志。
                    return self._ok(mid, self._tool_error(e, name))
                result = {"content": [{"type": "text",
                                       "text": json.dumps(out, ensure_ascii=False,
                                                          indent=1)}]}
            else:
                return self._err(mid, E_METHOD_NOT_FOUND,
                                 f"不支持的方法：{method}")
            return self._ok(mid, result) if mid is not None else None
        except Exception as e:                                  # noqa: BLE001
            return self._err(mid, -32000, user_error(e, "mcp.handle"))

    @staticmethod
    def _tool_error(exc: BaseException, name: str) -> dict:
        """把工具异常包装成 MCP 规定的 isError 结果。"""
        return {"content": [{"type": "text",
                             "text": user_error(exc, f"mcp.tool:{name}")}],
                "isError": True}

    # 出口收口见模块级 _model_safe()。

    def handle_batch(self, msg: list) -> Optional[dict]:
        """JSON-RPC 批量请求：逐条处理，全为通知则不回。"""
        responses = [r for r in (self.handle(m) for m in msg) if r is not None]
        return responses or None

    @staticmethod
    def _ok(mid, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}


def serve() -> None:
    """stdio 主循环：每行一个 JSON-RPC 请求，stdout 回一行响应。"""
    # 与 CLI 同理：这是**第三个入口**，同样从未接过日志文件。
    # MCP 尤其致命——stdout 是协议通道，stderr 常被 host（Claude Desktop
    # 等）原样采集进日志面板，traceback 连同本机路径会直接出现在别人的
    # 客户端里。转译层的"只进内部日志"在这里同样是空话。
    configure_logging()
    core = McpCore()
    lock = threading.Lock()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = core._err(None, E_PARSE, "解析失败：不是合法的 JSON")
        else:
            resp = core.handle_batch(msg) if isinstance(msg, list) else core.handle(msg)
        if resp is None:
            continue
        with lock:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
