#!/usr/bin/env python3
"""OmegaForge CLI — forge, run, arena, report.

Usage:
  python -m omegaforge.cli distill <source-path-or-text> [--out DIR]
                                 [--budget N] [--rounds N] [--gens N]
  python -m omegaforge.cli run     <genome.json> "<task>"
  python -m omegaforge.cli report  <output-dir>

Examples:
  python -m omegaforge.cli distill ./examples/sample_agent.py
  python -m omegaforge.cli distill "You are a UX research agent that ..."
  python -m omegaforge.cli run output/genome.json "Research: top AI trends"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .llm.client import LLMClient
from .core.budget import TokenBank, BudgetExceeded
from .core.errors import (UserError, configure_logging, user_error,
                          record_internal_error)
from .core.limits import (DEFAULT_BUDGET, MAX_BUDGET, MIN_BUDGET,
                          GENS_MAX, GENS_MIN, GENS_DEFAULT,
                          ROUNDS_MAX, ROUNDS_MIN, ROUNDS_DEFAULT)
from .core.validate import as_int
from .distill.engine import DistillEngine
from .agent.super_agent import SuperAgent
from .memory.kb import KnowledgeBase
from .memory.wiki import Wiki
from .memory.tasks import Tasks
from .skills.manager import SkillManager


class _CliHelpFormatter(argparse.HelpFormatter):
    """help 输出里的小节标题改成中文。

    argparse 的 "usage: " 前缀是硬编码常量，不走 gettext，只能在格式化器
    这一层换掉。
    """

    class _Section(argparse.HelpFormatter._Section):
        def format_help(self):
            out = super().format_help()
            # 小节标题后的冒号是硬编码半角，与其余中文标点不一致
            if out and self.heading:
                out = out.replace(f"{self.heading}:\n", f"{self.heading}：\n", 1)
            return out

    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(usage, actions, groups,
                                     "用法：" if prefix is None else prefix)


# argparse 内置错误文案 → 中文。命令行其余提示已全部是中文，报错若仍是
# 英文就与整体双标，而用户看到英文只会当成"程序坏了"。
_ARGPARSE_MSG_CN = (
    (r"the following arguments are required: (?P<v>.+)",
     "缺少必填参数：{v}"),
    (r"unrecognized arguments: (?P<v>.+)",
     "无法识别的参数：{v}"),
    (r"argument (?P<n>[\w-]+): invalid choice: (?P<v>\S+) "
     r"\(choose from (?P<c>.+)\)",
     "{n} 的取值无效：{v}（可选值：{c}）"),
    (r"invalid choice: (?P<v>\S+) \(choose from (?P<c>.+)\)",
     "取值无效：{v}（可选值：{c}）"),
    (r"argument (?P<n>\S+): invalid int value: (?P<v>.+)",
     "{n} 需要填写整数，当前填写的不是有效数值"),
    (r"invalid int value: (?P<v>.+)",
     "此处需要填写整数，当前填写的不是有效数值"),
    (r"ambiguous option: (?P<v>\S+) could match (?P<c>.+)",
     "选项写法有歧义：{v} 可能匹配 {c}"),
    (r"expected one argument", "此处需要填写一个值"),
)


def _cn_argparse_msg(message: str) -> str:
    import re as _re
    for pattern, cn in _ARGPARSE_MSG_CN:
        m = _re.fullmatch(pattern, message.strip())
        if m:
            return cn.format(**m.groupdict())
    # 未登记的模板保持原样：宁可留一句英文，也不能把报错内容改没了
    return message


class _CliParser(argparse.ArgumentParser):
    """中文 help 与中文报错的命令行解析器。

    子命令解析器由 add_subparsers 用同一类型创建，所以整套 help 与报错
    口径一致，不需要逐个改写。
    """

    def __init__(self, **kw):
        kw.setdefault("formatter_class", _CliHelpFormatter)
        super().__init__(**kw)
        # 两个默认分组的标题是硬编码英文，只能在实例上改
        self._positionals.title = "位置参数"
        self._optionals.title = "可选参数"
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "显示帮助信息并退出"

    def error(self, message: str):  # noqa: A003
        self.exit(2, f"{self.prog}：参数有误 —— {_cn_argparse_msg(message)}\n")


def _force_utf8() -> None:
    """Windows 控制台默认 cp1252，本项目横幅/日志含 Ω、→、⚠、═ 与中文，
    不强制 UTF-8 会在 print 时抛 UnicodeEncodeError。详见 scripts/sidecar_main.py 同款说明。
    """
    import io as _io
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception:
            pass
        try:
            setattr(sys, name, _io.TextIOWrapper(
                getattr(stream, "buffer", stream),
                encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


def _banner() -> None:
    _force_utf8()
    print("""
 Ω OMEGAFORGE — distill any agent. forge it stronger. spend less.
 ────────────────────────────────────────────────────────────────
""")


def _fail(msg: str) -> int:
    """统一失败出口：中文提示 + 非零退出码。

    为什么必须显式返回非零：例如 `task done ""` 打印 `completed: False`
    却返回 0，自动化流程会当成执行成功；`report <空目录>` 什么都不打印
    同样返回 0。两类都是"静默成功"——用户与自动化都拿不到失败信号。
    """
    print(f"⚠ {msg}", file=sys.stderr)
    return 1


def _gate(args) -> None:
    """数值闸门 —— 与服务端共用 core.limits，杜绝两套口径。

    统一走 as_int 而非各自比较：它同时挡住 bool、None、NaN、Infinity、
    字符串数字等非法形态，避免这些值穿透为服务端错误。
    """
    cmd = getattr(args, "cmd", None)
    if cmd == "task":
        # --priority 用 argparse 裸 type=int，与 distill 双标 ——
        #   --priority 99 / -5  → 被下游静默收敛成 P3，命令行不吭声
        #   --priority abc      → argparse 英文 usage 转储 + rc=2
        # 后者与其他子命令的中文提示完全不一致，前者则是"改了没告诉你"。
        args.priority = as_int({"priority": args.priority}, "priority", 2,
                               minimum=1, maximum=3, label="优先级")
        return
    if cmd != "distill":
        return
    payload = {"budget": args.budget, "rounds": args.rounds,
               "gens": args.gens}
    args.budget = as_int(payload, "budget", DEFAULT_BUDGET,
                         minimum=MIN_BUDGET, maximum=MAX_BUDGET,
                         label="预算")
    args.rounds = as_int(payload, "rounds", ROUNDS_DEFAULT,
                         minimum=ROUNDS_MIN, maximum=ROUNDS_MAX,
                         label="评测轮次")
    args.gens = as_int(payload, "gens", GENS_DEFAULT,
                       minimum=GENS_MIN, maximum=GENS_MAX,
                       label="进化代数")


def main(argv: list[str] | None = None) -> int:
    """入口外壳：只负责把异常收口成中文提示，业务仍在 _dispatch。

    未预期的编程错误同样收口，但**不掩盖**：完整调用栈先写进内部日志再
    给使用者一句中文。直接放它抛到栈顶的结果是，英文调用栈连同本机绝对
    路径打到标准错误输出——那是使用者看得见的通道——而日志文件里却什么
    都没留下，两头落空。

    为何在此配置日志：异常转译层会把完整调用栈写入内部日志，前提是日志已
    输出到文件。命令行作为独立入口需要显式接入，否则日志会直接打到标准
    错误输出。
    """
    configure_logging()
    try:
        return _dispatch(argv)
    except UserError as e:
        # UserError 文案已审校，原样透出
        return _fail(user_error(e))
    except BudgetExceeded as e:
        return _fail(user_error(e))
    except FileNotFoundError:
        # 不回显路径：完整本机路径属于内部信息，用户只需知道没找到
        return _fail("找不到对应的文件或技能，请确认路径/名称是否正确")
    except (OSError, ValueError, KeyError) as e:
        # JSON 损坏是 ValueError 子类；配置缺键是 KeyError
        return _fail(user_error(e))
    except Exception as e:
        # 只记日志、不回显异常原文：原文可能是英文且带本机路径。
        # 日志文件名可以告知——它是使用者自己数据目录下的文件，
        # 便于反馈问题时一并给出。
        record_internal_error(e, "cli.main")
        return _fail("程序遇到未预期的错误，详细信息已记入数据目录下的"
                     "日志文件 errors.log")


def _print_comparison(prev_path: str, report) -> None:
    """与历史版本比对：本版比上一版本强了多少、依据哪套题。

    为什么要显式打印而只给一个数字：**题不同则分数不可比**。
    直接给 delta 等于默许拿两把不同的尺子量出来的差当进步。
    """
    import os as _os
    import json as _json
    p = prev_path
    if _os.path.isdir(p):
        p = _os.path.join(p, "report.json")
    if not _os.path.isfile(p):
        print(f"\n⚠ 找不到历史报告：{p}")
        return
    try:
        with open(p, encoding="utf-8") as f:
            prev = _json.load(f)
    except (ValueError, OSError):
        print(f"\n⚠ 历史报告无法读取：{p}")
        return
    # 必须走 to_dict()：结论成立标记等三件套是 property，
    # 不在 __dict__ 里，直接传 __dict__ 会恒判为 False。
    r = DistillEngine.compare_reports(prev, report.to_dict())
    print("\n────────── COMPARED WITH PREVIOUS ──────────")
    if not r["comparable"]:
        print(f" 不可比      : {r['reason']}")
        print(f" 上一版本/本版 : {r['prev_score']} / {r['curr_score']}")
        print("═══════════════════════════════════════════════")
        return
    d = r["delta"]
    arrow = "↑ 更强" if d > 0 else ("↓ 更弱" if d < 0 else "→ 持平")
    print(f" 变化        : {d:+.2f}  {arrow}")
    print(f" 上一版本/本版 : {r['prev_score']:.2f} / {r['curr_score']:.2f}")
    print(f" 同一套题    : 是（指纹 {r['fingerprint'][:8]}）")
    print("═══════════════════════════════════════════════")


def _print_ablation(report) -> None:
    """打印基因消融归因。没跑消融（默认）就什么都不输出。"""
    ab = getattr(report, "ablation", None) or {}
    if not ab or not ab.get("variants"):
        return
    print("\n────────── GENE ABLATION ──────────")
    print(f" baseline      : {ab.get('baseline_score')}")
    if ab.get("aborted"):
        print(f" ⚠ {ab.get('note')}")
    for v in ab.get("variants", []):
        if "error" in v:
            print(f" · {v['gene'][:28]:<28} 未评出（{v['error']}）")
            continue
        mark = {"harmful": "有害", "beneficial": "有益",
                "neutral": "中性"}.get(v["verdict"], v["verdict"])
        print(f" · {v['gene'][:28]:<28} {v['delta']:+.2f}  {mark}")
    if ab.get("truncated"):
        print(" ⚠ 基因条数超过上限，仅消融了前几条")
    if ab.get("discriminative") is False:
        print(f" ⚠ {ab.get('note')}")
    elif not ab.get("comparable"):
        print(" ⚠ 当前消融含未去偏或被污染的样本，结论仅供参考")
    print("═══════════════════════════════════")



def _build_parser() -> _CliParser:
    """构造命令行解析器。

    单独成一个函数而不是内联在 _dispatch 里：帮助与报错文案属于用户可见
    内容，需要能被逐条遍历到，内联就无从核对。
    """
    ap = _CliParser(prog="omegaforge",
                    description="把源 Agent 的能力提炼成可运行的蒸馏体。")
    sub = ap.add_subparsers(dest="cmd", required=True, title="可用命令",
                            metavar="{distill,run,report,kb,wiki,task,skill,mcp}")

    d = sub.add_parser("distill", help="从源 Agent 提炼能力")
    d.add_argument("source", help="源材料：文件路径或直接粘贴的提示词正文")
    d.add_argument("--out", default="output", help="结果输出目录")
    # 刻意**不**写 type=int：argparse 在 _gate 之前解析，非法输入会先抛
    # 英文 usage 转储（例如 `--priority abc` → rc=2 + 整段 usage），与其余
    # 子命令的中文提示双标。交给 _gate 统一用 as_int 判定，CLI 与服务端
    # 就是同一套口径、同一套文案。
    d.add_argument("--budget", default=DEFAULT_BUDGET, help="预算上限")
    d.add_argument("--rounds", default=ROUNDS_DEFAULT,
                   help="每代评测的用例数")
    d.add_argument("--gens", default=GENS_DEFAULT,
                   help="进化代数上限")
    # 冻结评测集：给定时跳过出题，沿用既有的一套题。
    # 跨次比较只在题不变时才有意义——题每次重新生成的话，
    # "本版比上一版本强"是拿两把不同的尺子量出来的，不成立。
    d.add_argument("--eval-set", default="",
                   help="沿用已冻结的评测集文件")
    # 跨版本比对：给上一次的输出目录，回答"本版比上一版本强了多少"。
    # 只在**同一套题**下才给差值——题不同则分数不可比，给差值是假精确。
    d.add_argument("--compare", default="",
                   help="与上一次的结果目录做跨版本比对")
    # 基因消融归因：逐条去掉基因重测，回答"哪条基因在拖后腿"。
    # 默认不开——每个变体都要重跑合成 + 双顺序评分。
    d.add_argument("--ablate", action="store_true",
                   help="逐条去掉基因重测，归因分数变化")
    # 对照组提示词：源材料里提取不到可用的系统提示词时，对照组会改用简化
    # 对照，结论必然不成立。这是唯一能补救的入参，三个入口都要提供它——
    # 否则结论里"请提供源 Agent 原始提示词"就是一条无处可填的指引。
    d.add_argument("--baseline-prompt", default="",
                   help="指定对照组使用的源 Agent 系统提示词；源材料里"
                        "提取不到时，填它才能得到可采信的结论")

    r = sub.add_parser("run", help="用蒸馏体执行一个任务")
    r.add_argument("genome", help="蒸馏体文件路径")
    r.add_argument("task", help="要执行的任务")

    rep = sub.add_parser("report", help="打印蒸馏报告与用量账本")
    rep.add_argument("outdir", help="结果输出目录")

    kb = sub.add_parser("kb", help="个人知识库")
    kb.add_argument("op", choices=["add", "search", "list"], help="操作")
    kb.add_argument("text", nargs="?", help="内容或检索词")
    kb.add_argument("--title", help="标题")

    wk = sub.add_parser("wiki", help="个人词条库（支持反向链接）")
    wk.add_argument("op", choices=["save", "get", "search", "list"], help="操作")
    wk.add_argument("slug", nargs="?", help="词条标识")
    wk.add_argument("--title", help="标题")
    wk.add_argument("--body", help="正文")

    tk = sub.add_parser("task", help="待办清单")
    tk.add_argument("op", choices=["add", "done", "list"], help="操作")
    tk.add_argument("text", nargs="?", help="任务内容")
    tk.add_argument("--priority", default=2, help="优先级，1 到 3")

    sk = sub.add_parser("skill", help="安装与调用技能")
    sk.add_argument("op", choices=["install", "list", "invoke"], help="操作")
    sk.add_argument("arg", nargs="?", help="技能包路径或技能名称")
    sk.add_argument("--context", help="调用技能时附带的上下文")

    sub.add_parser("mcp", help="以 MCP 服务方式运行（标准输入输出）")
    return ap


def _dispatch(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    _banner()

    llm = LLMClient()
    # 演示模式提示只对会调用模型的命令有意义：kb / wiki / task / skill
    # 读的是磁盘上真实存在的条目，与模型无关。在这些命令上提示"结果不来
    # 自真实模型"会让使用者以为清单不可信，并去做一个对本命令毫无作用的
    # 设置。
    if llm.mock_mode and args.cmd in ("distill", "run"):
        print("⚠ 当前未配置模型服务，处于演示模式：结果不来自真实模型。"
              "请在设置页填写接口地址与密钥后重试。\n")

    # 闸门必须排在演示模式提示之后：校验失败与"没配模型"无因果关系，
    # 若提示先于横幅打印，用户会把"找不到文件"读成"没配模型"的后果。
    _gate(args)

    if args.cmd == "distill":
        bank = TokenBank(args.budget)
        eval_set = (DistillEngine.load_eval_set(args.eval_set)
                    if getattr(args, "eval_set", "") else None)
        engine = DistillEngine(llm, bank, arena_rounds=args.rounds,
                               max_generations=args.gens, eval_set=eval_set)
        genome, report = engine.distill(args.source, output_dir=args.out,
                                        ablate=getattr(args, "ablate", False),
                                        baseline_prompt=getattr(
                                            args, "baseline_prompt", ""))
        print("\n════════════ DISTILLATION VERDICT ════════════")
        print(f" genome        : {args.out}/genome.json")
        print(f" verdict       : {report.verdict}")
        print(f" distilled     : {report.final_score:.2f} / 10")
        print(f" baseline      : {report.baseline_score:.2f} / 10")
        print(f" generations   : {report.generation}")
        print(f" system tokens : {genome.est_system_tokens} "
              f"(was est. {genome.baseline_tokens_per_task})")
        print(f" tokens spent  : {bank.total_spent} / {bank.budget}")
        for ph, tk in bank.ledger_by_phase().items():
            print(f"   · {ph:<18} {tk:>8}")
        print("═══════════════════════════════════════════════")
        cmp_dir = getattr(args, "compare", "") or ""
        if cmp_dir:
            _print_comparison(cmp_dir, report)
        _print_ablation(report)
        return 0

    if args.cmd == "run":
        if not os.path.isfile(args.genome):
            return _fail("找不到该 genome 文件，请确认路径是否正确")
        bank = TokenBank(200_000)
        try:
            agent = SuperAgent.from_genome_file(args.genome, llm, bank)
        except json.JSONDecodeError:
            # 损坏的 genome 被说成"模型返回了无法解析的内容"。
            # 这是**读用户文件**失败，跟模型毫无关系——通用映射把锅甩给了
            # 模型，用户据此去查接口，实际该做的是重新导出 genome。
            return _fail("该文件不是有效的 genome（内容不是合法 JSON），"
                         "请重新导出后再试")
        res = agent.run(args.task)
        print(f"agent : {agent.name}")
        print(f"ok    : {res.ok}   tokens: {res.tokens_used}   "
              f"steps: {' -> '.join(res.steps_executed) or '-'}")
        print("─" * 50)
        print(res.answer)
        return 0 if res.ok else 1

    if args.cmd == "report":
        rp = os.path.join(args.outdir, "report.json")
        gp = os.path.join(args.outdir, "genome.json")
        found = False
        # 用 with 而非 json.load(open(...))：后者不关句柄，且文件损坏时
        # 抛的是原始 JSONDecodeError 栈（已由 main 收口成中文）。
        if os.path.exists(rp):
            found = True
            with open(rp, encoding="utf-8") as f:
                try:
                    rep = json.load(f)
                except json.JSONDecodeError:
                    # 与 run 同理：产物损坏是"文件坏了"，不是模型的问题
                    return _fail("report.json 已损坏，无法解析，请重新生成")
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        if os.path.exists(gp):
            found = True
            with open(gp, encoding="utf-8") as f:
                try:
                    g = json.load(f)
                except json.JSONDecodeError:
                    return _fail("genome.json 已损坏，无法解析，请重新生成")
            print("\n— genome genes —")
            for k in ("persona_genes", "tool_genes", "workflow_genes",
                      "upgrade_genes"):
                print(f"{k}: {g.get(k)}")
        if not found:
            # 目录不存在时**什么都不打印且返回 0**，
            # 用户以为命令跑通了，实际一个产物都没读到。
            return _fail("该目录下没有找到 report.json 或 genome.json，"
                         "请确认是否为蒸馏产物目录")
        return 0

    if args.cmd == "kb":
        db = KnowledgeBase()
        if args.op == "add":
            if not args.text or not args.text.strip():
                # `kb add` 打印英文 `text required` 且走 stdout
                # —— 与全部走 _fail 的中文提示不一致，stderr 也没有信号。
                return _fail("请填写要存入的内容")
            print("id:", db.add(args.title or args.text[:60], args.text))
        elif args.op == "search":
            for r in db.search(args.text or ""):
                print(f"[{r['score']}] {r['title']} — {r['excerpt'][:80]}")
        else:
            for d in db.all():
                print(f"[{d['type']}] {d['title']}  (id={d['id']})")
        return 0

    if args.cmd == "wiki":
        w = Wiki(kb=KnowledgeBase())
        if args.op == "save":
            if not (args.slug and args.title):
                return _fail("请同时填写词条标识（slug）和标题（--title）")
            print("saved:", w.save(args.slug, args.title, args.body or ""))
        elif args.op == "get":
            # 词条不存在时不能什么都不打印还返回 0：
            # 那属于静默成功，用户与脚本都会以为读到了内容。
            if not args.slug:
                return _fail("请填写要查看的词条标识（slug）")
            pg = w.get(args.slug)
            if not pg:
                return _fail(f"没有找到词条「{args.slug}」，请确认标识是否正确")
            print(pg["body"], "\n— backlinks:", pg["backlinks"])
        elif args.op == "search":
            for r in w.search(args.slug or ""):
                print(f"[{r['score']}] {r['title']} ({r['slug']})")
        else:
            for p in w.pages():
                print(f"{p['slug']}  — {p['title']}")
        return 0

    if args.cmd == "task":
        t = Tasks()
        if args.op == "add":
            if not args.text:
                return _fail("请填写任务内容")
            it = t.add(args.text, args.priority)
            print(f"added {it['id']}: {it['text']} (P{it['priority']})")
        elif args.op == "done":
            # `task done ""` 打印 `completed: False`
            # 却返回 0——用户以为完成了，自动化流程也以为完成了。
            # 失败必须给非零退出码，否则自动化拿不到任何信号。
            if not args.text:
                return _fail("请指定要完成的任务名称")
            ok = t.complete(args.text)
            print(f"completed: {ok}")
            if not ok:
                return _fail("没有找到匹配的任务，请确认名称是否正确")
        else:
            for i in t.list():
                print(f"{i['id']}  P{i['priority']}  {i['text']}")
            # 直接 print(dict) 会把 Python 字面量打到终端上：使用者看见
            # 一行 {'total': 2, ...}，不像产品输出，像调试残留。
            st = t.stats()
            print(f"共 {st.get('total', 0)} 条，"
                  f"待办 {st.get('pending', 0)} 条，"
                  f"已完成 {st.get('done', 0)} 条")
        return 0

    if args.cmd == "skill":
        m = SkillManager()
        if args.op == "install":
            if not args.arg:
                return _fail("请提供技能目录路径")
            print(m.install(args.arg))
        elif args.op == "invoke":
            # `skill invoke` 空参数报"找不到对应的文件或技能" ——
            # 用户没填和填错了是两回事，混成一句会让人去查技能名而不是补参数。
            if not args.arg or not args.arg.strip():
                return _fail("请填写要调用的技能名")
            print(m.invoke(args.arg, args.context or ""))
        else:
            items = m.list()
            # 空列表必须明说：什么都不打印时，用户分不清"确实没有技能"
            # 与"列举功能坏了"，后者会被当成前者带过去。
            if not items:
                print("当前没有已安装的技能。用 omegaforge skill install <路径> 安装。")
                return 0
            for s in items:
                print(f"{s['name']} v{s['version']} — {s['description']}")
        return 0

    if args.cmd == "mcp":
        from .mcp_server import serve as mcp_serve
        mcp_serve()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
