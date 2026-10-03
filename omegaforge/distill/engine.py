"""OmegaForge DistillEngine — 复刻任何 Agent 并使其可验证地更强。

Pipeline (one-shot, budgeted):
  1. INGEST   : SourceAgentLoader -> SourceSignals      (0 token)
  2. EXTRACT  : signals -> SourceSpec (structured)      (1 call, fast)
  3. COMPRESS : SourceSpec -> Genome (persona/tool/workflow
                genes, dedup, strip filler)              (1 call, fast)
  4. SYNTHESIZE: Genome -> 编译后的系统提示词与工具
                + workflow + upgrade_genes               (1 call, main)
  5. GEN_EVAL : derive exam from mission                 (1 call, fast)
  6. ARENA    : original-style vs distilled, LLM-judge   (2*N calls)
  7. EVOLVE   : if lost/tied -> critique -> mutate genes -> re-synthesize
                (max N generations, stop when win or budget)

Prompts use <<PLACEHOLDER>> tokens + .replace (JSON braces safe).
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass, field

from ..llm.client import LLMClient
from ..core.budget import BudgetExceeded, TokenBank
from ..domain.baseline import Baseline, build_baseline
from ..core.errors import user_error, UserError
from ..tools.provenance import (wrap, scan, TAG_TEXT,
                                scan_judge_manipulation, wrap_candidate)
from .loader import SourceAgentLoader, SourceSignals
from .genome import Genome

# 结论的中文展示：verdict 是内部枚举值，直接上屏等于暴露开发术语。
# 与前端 format.ts 的 VERDICT_TEXT 保持一一对应。
VERDICT_TEXT = {
    "win": "蒸馏体胜出",
    "tie": "双方持平",
    "loss": "源 Agent 胜出",
    "budget-exhausted": "预算耗尽，未完成对比",
}



@dataclass
class SourceSpec:
    """Structured understanding of the source agent (step-2 artifact)."""
    name: str
    role: str
    mission: str
    persona: str
    tools: list
    workflow: list
    io: dict
    failure_modes: list
    quality_bars: list
    raw_summary: str = ""
    source_flags: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)


@dataclass
class DistillReport:
    source_signals: str
    generation: int = 0
    final_score: float = 0.0
    baseline_score: float = 0.0
    verdict: str = "pending"          # win|tie|loss|budget-exhausted
    judge_reasons: list = field(default_factory=list)
    evolution_notes: list = field(default_factory=list)
    # 对照组可信度：决定"更强"这个结论能不能成立
    baseline_kind: str = ""           # source_提示词|provided|简化对照
    baseline_comparable: bool = False
    baseline_note: str = ""
    # 评分污染：被测方是否试图影响裁判。
    # 暴露而不是剔除——静默剔除会把"样本不足"藏起来，用户需要知道
    # 这轮结论建立在多少条可信样本上。
    arena_cases: int = 0
    contaminated_cases: int = 0
    debiased_cases: int = 0
    trust_note: str = ""
    # 裁判链（adjudication chain）：谁出的题、谁答的、谁判的。
    # 缺了这三项，"更强"就是无源之水——用户无从判断结论是由同一模型作答与评分的
    # 还是被测出来的。见 _explain_self_certification。
    question_source: str = ""      # llm|user|builtin
    answer_model: str = ""
    judge_model: str = ""
    # 出题侧的闭环（比裁判同源更隐蔽，见「出题侧自证」标记）：
    # 题目和评分标准都由被测方自己生成，换裁判模型也绕不开。
    question_model: str = ""       # 出考题的模型；builtin/user 时为空
    rubric_source: str = ""        # llm|builtin|user —— 实际参评用例的标准来源
    # 自进化的三条腿：可版本化、可评测、可回滚。
    # 缺了回滚，"进化"就只是"越改越糟"——未经改善的版本必须退回上一版本，
    # 否则交付的可能是历代中最差的一代。
    best_generation: int = 0       # 最终交付的那一代
    generations_run: int = 0       # 实际跑过多少代
    rolled_back: bool = False      # 是否发生过回滚（进化后未改善）
    # 基因消融归因：哪条基因在拖后腿。默认空 = 没跑消融（默认不开，
    # 因为每个变体都要重跑 synthesize + 双顺序评分，成本不低）。
    ablation: dict = field(default_factory=dict)
    # 评测集来源：generated（本版自动生成）/ provided（沿用既有评测集）。
    # 跨次比较只在 provided 下成立——题每次重新生成的话，
    # "本版比上一版本强"根本无从谈起（分数不可比）。
    eval_set_source: str = ""
    eval_set_cases: int = 0        # 冻结用例条数（实际写入文件的条数）
    # 评测集指纹：跨版本比对的唯一凭据。
    # 没有它，"比上次强了多少"可能是拿两套不同的题量出来的——
    # 题不同则分数不可比，必须拒绝给差，而不是给一个看似精确的数字。
    eval_set_fingerprint: str = ""

    @property
    def exam_self_authored(self) -> bool:
        """考题或评分标准是否由被测方自己定的（出题侧由同一模型作答与评分）。

        与裁判自证标记（裁判 vs 作答）互补，且更隐蔽：
        换一个第三方裁判只能切断"谁来判"，切不断"考什么、按什么标准判"。
        例如：judge=claude-haiku、answer=gpt-4o-mini（已非由同一模型作答与评分）时，
        两条裁判指令里**全都**含被测方自定的评分标准——裁判再独立，
        也是在按被测方定的尺子打分。

        评分标准自定尤其致命：rubric 直接决定分数的含义，被测方只要
        把标准写成自己的强项（"结构清晰""分点作答"），裁判越认真执行，
        分数越偏。这不是噪声，是系统性的，且换裁判无法修正。

        fail-closed：来源未知按"自定"处理。
        """
        if not self.rubric_source:
            return True
        if self.rubric_source == "llm":
            return True
        # 题目由被测方出：天然贴合它自己的表达与知识边界
        return bool(self.question_model) and \
            self.question_model == self.answer_model

    @property
    def self_certified(self) -> bool:
        """裁判与作答是否同源（由同一模型作答与评分闭环）。

        LLM judge 已知存在**自偏好**：倾向于给自己（或同族模型）的输出
        打高分。这不是随机误差，不会像位置偏差那样靠多次平均抵消——
        它系统性地作用于每一个用例，而且完全不可见。

        默认配置下作答与裁判可能由同一模型承担，此时"蒸馏体更强"有可能
        只是模型更偏好自己的输出。产物因此必须记录裁判来自哪个模型，
        供使用者复核结论来源。

        fail-closed：模型信息缺失时按"未排除自我评分"处理。理由——空值只可能
        来自"忘了填"，而"忘了填"不该被当成"已排除"。
        """
        if not self.answer_model or not self.judge_model:
            return True
        return self.answer_model == self.judge_model

    # 结论可信度三件套是 property（刻意不存字段：存字段的默认值会被
    # 当成"已排除"，正是前面反复修的静默失效），但它**不在 __dict__ 里**，
    # 于是 json.dumps(self.__dict__) 落盘时把三项全部丢掉。
    # 后果：产物 report.json 里 结论是否成立 恒为 None，跨版本比对
    # 读历史报告时永远判定"有一侧的结论本身不成立"——功能整体不可用，
    # 而且用户拿产物根本无从复核结论可信度。
    _DERIVED_FIELDS = ("claim_valid", "self_certified", "exam_self_authored")

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        for k in self._DERIVED_FIELDS:
            data[k] = bool(getattr(self, k))
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def claim_valid(self) -> bool:
        """'可验证地更强'这一结论是否站得住。

        两个条件缺一不可：
          1. 对照组真实可比（源 agent 原始 提示词 或用户指定对照）——
             简化对照 对照下即便获胜也只是演示。
          2. 评分未被被测方污染——若被测方的答案里含自我标榜或打分
             要求，胜利有可能是它自己写给自己的，而非被测出来的。
          3. 至少真实评估过一个用例——零样本下"更强"没有支撑，
             verdict 目前在竞技场循环内赋值，实战路径走不到这个状态，
             但这不该依赖"恰好走不到"来保证。
          4. 裁判与作答不同源——同源即自我评分闭环，模型在给自己的输出
             打分。污染影响的是个别用例，自我评分影响的是**每一条**且不可见，
             因此同样设为硬闸门。
          5. 考题与评分标准不得由被测方自定——这是第 4 条的**前置**漏洞：
             换第三方裁判只换了"谁来判"，而"考什么、按什么标准判"仍然
             由被测方说了算。即便裁判与作答不同源，裁判指令中仍可能携带
             被测方自定的评分标准。不设这条，使用者
             换个裁判模型就能拿到一个看似可信、实则仍自说自话的结论。

        第 2 条不可省：只检查第 1 条的话，"被测方 10.0 / 对照组 1.0"
        这种明显被操纵的结果照样能拿到成立的结论。
        第 4 条同理：默认配置下裁判与作答本就是同一模型，不设闸门则
        这条链路产出的"更强"永远是自我评分的，而用户毫无察觉。
        """
        return (self.verdict == "win" and self.baseline_comparable
                and self.contaminated_cases == 0
                and self.arena_cases > 0
                and self.debiased_cases == self.arena_cases
                and not self.self_certified
                and not self.exam_self_authored)


EXTRACT_SPEC_PROMPT = """You are AgentArchaeologist. Reconstruct a precise SPEC of the agent
from the signals below. Output ONLY JSON:
{"name": str, "role": str, "mission": str, "persona": str,
 "tools": [{"name": str, "description": str, "params": {}}],
 "workflow": [{"id": str, "actor": str, "action": str, "input": str, "output": str}],
 "io": {"input": str, "output": str},
 "failure_modes": [str], "quality_bars": [str]}
Be faithful to the source. Do not invent capabilities not implied by signals.

SIGNALS:
<<SIGNALS>>"""

COMPRESS_PROMPT = """Compress this agent SPEC into a Genome. Keep semantic essentials,
drop filler/boilerplate/redundant politeness. Output ONLY JSON:
{"name": str, "mission_one_liner": str,
 "persona_genes": [3-6 short traits],
 "tool_genes": ["tool_name(params) ..."],
 "workflow_genes": ["step phrase"],
 "upgrade_genes": [2-4 concrete improvements an elite engineer would add],
 "est_system_tokens": int}
Upgrade genes must be actionable, e.g. "verify every citation before output",
"cap answers at N words", "self-check schema before returning".

SPEC:
<<SPEC>>"""

SYNTHESIZE_PROMPT = """Compile this Genome into a production agent. Output ONLY JSON:
{"system_prompt": str (complete, self-contained, <=350 words, imperative voice,
  embeds quality bars & failure guards & workflow),
 "tools": [tool names], "workflow": [ordered step names],
 "design_notes": str (<=50 words)}
The system prompt must be DENSER and STRONGER than a naive rewrite: no fluff,
each sentence either enables a capability or prevents a failure mode.

GENOME:
<<GENOME>>"""

GEN_EVAL_PROMPT = """Design a compact exam for an agent with this mission. Output ONLY JSON:
{"cases": [{"id": str, "input": str (realistic task input),
            "rubric": [3-4 gradable criteria]}]}
6 cases: 4 typical, 1 adversarial edge, 1 stress (long/ambiguous). Judgeable
objectively by an LLM using the rubric.

MISSION: <<MISSION>>
ROLE: <<ROLE>>"""

ARENA_PROMPT = """You are a strict impartial judge. Two agents answered the same task.
Score each 0-10 on: task_completion, correctness, efficiency, robustness,
polish. Output ONLY JSON:
{"scores": {"A": {"task_completion": 0, "correctness": 0, "efficiency": 0,
                  "robustness": 0, "polish": 0},
            "B": {"task_completion": 0, "correctness": 0, "efficiency": 0,
                  "robustness": 0, "polish": 0}},
 "reason": str(<=40 words), "winner": "A" | "B" | "tie"}

TASK: <<TASK>>
RUBRIC: <<RUBRIC>>

ANSWER A:
<<ANSWER_A>>

ANSWER B:
<<ANSWER_B>>"""

CRITIQUE_PROMPT = """The distilled agent (B) failed to beat the original (A). Compare and
produce targeted gene mutations. Output ONLY JSON:
{"deltas": [3-5 concrete, prompt-level fixes, each <=25 words]}

TASK: <<TASK>>
JUDGE REASON: <<REASON>>
ANSWER A: <<ANSWER_A>>
ANSWER B: <<ANSWER_B>>"""


def _sanitize(obj):
    """Deep-clean a parsed object: models sometimes emit `...` placeholders
    which ast.literal_eval turns into Ellipsis — replace recursively."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if obj is Ellipsis:
        return ""
    return obj


def _avg_scores(raw) -> float:
    """对 judge 的一维评分取平均——只统计真正的数字维度。

    为什么不能写 sum(...)/len(d)：
      judge 常返回 {"task_completion": 8, "correctness": "good",
                   "efficiency": 7, "robustness": null, "polish": 9}
      5 个维度里只有 3 个是数字。若除以 len(d)=5，得 24/5=4.80，
      而真实水平是 24/3=8.00 —— **分数被系统性压低 40%**。
      这个数是"蒸馏体是否更强"的唯一依据，压低它等于扭曲产品结论。

    另外两个必须挡住的点：
      - d 不是 dict（模型把 A 写成字符串/数字/数组/空）→ 原有写法
        .values() 直接 AttributeError，整轮蒸馏崩溃
      - bool 是 int 的子类，True 会被当成 1 分计入，语义上是脏值
    """
    if not isinstance(raw, dict):
        return 0.0
    vals = [v for v in raw.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _as_token_count(raw) -> int:
    """把模型给出的 token 估算收敛为非负整数。

    模型给出的估算可能是字符串、空值或负数：字符串参与数值比较会中断
    蒸馏流程，空值同理。负数归零——负数 token 没有语义。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _as_gene_list(raw, limit: int = 12) -> list:
    """把模型给出的基因列表收敛为字符串列表。

    `extend` 对字符串会按字符拆分：`{"deltas": "abcdef"}` 直接扩展会
    变成六个单字符"基因"，且不产生任何报错，基因库随之被污染。
    dict 只取键，同样失真。
    """
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    out = []
    for it in items:
        if isinstance(it, str):
            s = it.strip()
        elif isinstance(it, (int, float)) and not isinstance(it, bool):
            s = str(it)
        else:
            continue          # dict / list / None / bool 一律丢弃
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _uniq(tags) -> list:
    """标签去重并保序（顺序稳定，产物可验证）。"""
    out = []
    for t in tags or []:
        if t and t not in out:
            out.append(t)
    return out


def _contam(one: dict | None, side: str) -> list:
    """取单侧评分的污染标签。

    用 .get 层层兜底而不是直接索引：_judge_once 是可被替换的接缝
    （测试模拟对象、第三方扩展都可能替换它），模拟对象返回结果里没有
    contamination 字段时不该让整轮评测崩掉——污染标记是增强信息，
    拿不到就不标，但它绝不能成为新的失败点。
    """
    c = (one or {}).get("contamination") or {}
    v = c.get(side) or []
    return [v] if isinstance(v, str) else list(v)


# 消融归因：变体数上限与噪声阈值。
# 上限是预算护栏：每个变体都要重跑 synthesize + 双顺序评分，
# 没有上限的话，基因稍多就会把整轮预算烧光。
ABLATION_MAX_VARIANTS = 6
# 小于此差值视为噪声。纯内在纠错的净收益在缺少可对照的标准答案时可能为负，
# 把噪声当信号会持续接受其实没用的基因。
ABLATION_MIN_GAIN = 0.05


class DistillEngine:
    def __init__(self, llm: LLMClient, bank: TokenBank,
                 arena_rounds: int = 6, max_generations: int = 3,
                 verbose: bool = True, baseline: Baseline | None = None,
                 on_phase=None, task: str = "", eval_set: list | None = None,
                 baseline_prompt: str = ""):
        self.llm = llm
        self.bank = bank
        self.arena_rounds = arena_rounds
        self.max_generations = max_generations
        self.verbose = verbose
        # 用户在界面上填写的评测任务。例如：前端一直在发这个字段，
        # 但后端从未读取 —— 不进 meta、不进引擎、不参与评测，
        # 等于界面上摆了个输入框骗人。这里让它真正参与对照评测。
        self.task = (task or "").strip()
        # 对照组：None 时由源材料推导（build_baseline）
        self.baseline: Baseline | None = baseline
        # 评分污染统计：本版评测有多少用例的答案含操纵痕迹。
        # 暴露出来的目的不是剔除——静默剔除会把"样本不足"藏起来——
        # 而是让用户知道这轮结论建立在多少条可信样本上。
        self.arena_cases = 0
        self.arena_contaminated = 0
        # 完成双顺序去偏的用例数。与污染分开统计：污染是"证据可疑"，
        # 未去偏是"证据偏弱"——两者都不足以支撑「更强」，但成因不同，
        # 给用户的原因说明必须能区分开。
        self.arena_debiased = 0
        # 考题来源：llm（自动出题）/ builtin（兜底标准题）/ user（用户指定）。
        # 记下来是因为"题是谁出的"决定了这道题是否可能偏向被测方——
        # 同源模型出的题天然更贴合它自己的表达方式。
        self.question_source = ""
        # 出题侧闭环：考题由哪个模型出、实际参评用例的评分标准来自谁。
        # 与 question_source（题目来源）分开是因为——题目来源只是"llm 还是
        # 内置"，而这里要记的是**模型身份**和**评分标准的来源**，用于
        # 判断"换裁判能否切断闭环"。
        self.question_model = ""
        self.rubric_source = ""
        # 每个用例的出身：input -> (题目来源, 评分标准来源)。
        # 记 input 而不是给 case 塞内部字段，是因为 case 会原样写进报告，
        # 塞私有键会把内部痕迹漏给用户。
        # 竞技场按 arena_rounds 截断，实际参评的可能只是前几条——
        # 因此标准来源必须按**真正被评估的**用例统计，而不是生成的全部。
        self._case_origin = {}
        # 阶段回调 on_phase(phase_name)：让调用方能驱动真实进度条，
        # 而不是让前端对着一个永远 0% 的假进度发呆。
        self.on_phase = on_phase
        # 冻结评测集：None 表示本版自动生成。给定时跳过出题，
        # 题目与评分标准都由使用者提供——这同时切断了**出题侧由同一模型作答与评分**
        # （换裁判模型也绕不开的那层偏差）。
        self.eval_set = list(eval_set) if eval_set else None
        # 用户显式指定的对照组 提示词。不为 None 时优先于源材料推导，
        # 是源材料里没有可提取的提示词时**唯一**能把结论变可信的手段。
        self.baseline_prompt = (baseline_prompt or "").strip()

    def _enter(self, phase: str) -> None:
        if self.on_phase:
            try:
                self.on_phase(phase)
            except Exception:                       # noqa: BLE001
                pass        # 进度上报失败不应中断蒸馏

    # -- helpers -------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[DistillEngine] {msg}")

    def _chat_json(self, prompt: str, phase: str, model: str,
                   max_tokens: int = 2048) -> dict:
        self.bank.charge_estimate(phase, model, 800)
        last_err: Exception = ValueError("no attempt")
        for attempt in range(2):
            nudge = "" if attempt == 0 else (
                "\n\nIMPORTANT: your previous reply was not valid JSON. "
                "Reply with ONE raw JSON object only — no prose, no "
                "markdown fences, no explanation.")
            r = self.llm.chat("You are OmegaForge distillation core. "
                              "Output ONLY valid JSON.", prompt + nudge,
                              model=model, max_tokens=max_tokens,
                              json_mode=True)
            self.bank.charge(phase, r.model, r.prompt_tokens,
                             r.completion_tokens)
            try:
                return self._safe_json(r.text)
            except (json.JSONDecodeError, ValueError) as e:
                last_err = e
                self._log(f"  模型返回的内容不是合法 JSON，正在用更严格的"
                          f"要求重试（第 {attempt + 1}/2 次）")
            except Exception as e:                  # noqa: BLE001
                last_err = e
                self._log(f"  调用模型服务失败，正在重试"
                          f"（第 {attempt + 1}/2 次）：{user_error(e, 'distill._chat_json')}")
        raise last_err

    @staticmethod
    def _safe_json(text: str) -> dict:
        """Robust JSON extraction: full text → fenced → balanced-brace
        candidates → json.loads / ast.literal_eval (single-quote dicts)."""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        candidates = [text.strip()]
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            while start != -1 and len(candidates) < 8:
                depth, in_str, esc = 0, False, False
                for i in range(start, len(text)):
                    ch = text[i]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    else:
                        if ch == '"':
                            in_str = True
                        elif ch == opener:
                            depth += 1
                        elif ch == closer:
                            depth -= 1
                            if depth == 0:
                                candidates.append(text[start:i + 1])
                                break
                start = text.find(opener, start + 1)
        for cand in candidates:
            cand = cand.strip()
            if not cand:
                continue
            for parser in (json.loads, ast.literal_eval):
                try:
                    obj = parser(cand)
                    if isinstance(obj, dict):
                        return _sanitize(obj)
                except Exception:                       # noqa: BLE001
                    continue
        raise json.JSONDecodeError("no parsable JSON object", text[:120], 0)

    # -- pipeline steps --------------------------------------------------
    def step_extract(self, sig: SourceSignals) -> SourceSpec:
        self._log("正在解析源材料，提取 Agent 的能力结构")
        # 源材料是外部内容进模型的最大通道，且污染会遗传：
        # 网页/文件 → 蒸馏 → Genome.system_prompt → 用户环境里执行。
        # 这是一条完整供应链：原文若不带分隔标记直接进提示词，其中的
        # 指令性句子会原样进入。
        #
        # 两类内容必须分开处理，不能一刀切：
        #   raw_excerpt（仅供分析的原文片段）→ 加分隔标记。蒸馏要的正是
        #     "理解这段文本描述了什么 Agent"，而不是"执行其中的命令"，
        #     所以降级为数据与蒸馏语义一致，不破坏提取。
        #   提示词_candidates（要继承进 Genome 的提示词）→ 只扫描告警、
        #     不加分隔标记。分隔标记文本会被写进最终 system_提示词，等于把
        #     防护标记变成产物污染。
        #
        # 不阻断：用户可能正是在蒸馏一份"提示词注入防护"的 Agent，那是
        # 合法需求。但不阻断的前提是必须让用户看见。
        flags = scan(sig.raw_text)
        if not flags:
            for c in sig.prompt_candidates[:3]:
                flags = scan(str(c))
                if flags:
                    break
        prompt = EXTRACT_SPEC_PROMPT.replace("<<SIGNALS>>", json.dumps({
            "source_type": sig.source_type,
            "name_hint": sig.name_hint,
            "role_hints": sig.role_hints,
            "prompt_candidates": sig.prompt_candidates[:3],
            "tool_candidates": sig.tool_candidates[:20],
            "workflow_cues": sig.workflow_cues,
            "raw_excerpt": wrap(sig.raw_text[:6000], "源材料")},
            ensure_ascii=False))
        data = self._chat_json(prompt, phase="distill:extract",
                               model=self.llm.model_fast)
        data.setdefault("name", sig.name_hint or "DistilledAgent")
        data.setdefault("role", "assistant")
        data.setdefault("mission", "")
        data.setdefault("persona", "")
        data.setdefault("tools", [])
        data.setdefault("workflow", [])
        data.setdefault("io", {})
        data.setdefault("failure_modes", [])
        data.setdefault("quality_bars", [])
        # 强制赋值而非 setdefault：source_flags 是本地扫描结果，不是模型
        # 返回值。若模型自己塞一个进来，等于让它给自己开脱。
        data["source_flags"] = flags
        summary = sig.summary()
        if flags:
            cn = "、".join(TAG_TEXT.get(t, t) for t in flags)
            summary += f"；注意：源材料中检测到可疑句式（{cn}）"
            self._log(f"  源材料含可疑句式（{cn}）——已按数据降级处理，"
                      f"请确认来源可信后再使用蒸馏产物")
        data["raw_summary"] = summary
        return SourceSpec(**{k: v for k, v in data.items()
                             if k in SourceSpec.__annotations__})

    def step_compress(self, spec: SourceSpec, lineage: str,
                      fingerprint: str) -> Genome:
        self._log("正在把能力结构压缩为可继承的基因组")
        prompt = COMPRESS_PROMPT.replace(
            "<<SPEC>>", json.dumps(spec.__dict__, ensure_ascii=False))
        data = self._chat_json(prompt, phase="distill:compress",
                               model=self.llm.model_fast)
        g = Genome(
            # name 必须收敛为非空字符串。例如：模型返回 {"name": null} 时
            # data.get("name", spec.name) 拿到 None（键存在、值为空，
            # 默认值不生效），随后 step_gen_eval 的
            # .replace("<<ROLE>>", g.name) 抛 TypeError:
            # replace() argument 2 must be str, not None —— 整轮蒸馏崩溃。
            name=(_as_gene_list([data.get("name")]) or
                  _as_gene_list([spec.name]) or ["DistilledAgent"])[0],
            mission_one_liner=str(data.get("mission_one_liner") or
                                  spec.mission or ""),
            source_fingerprint=fingerprint, lineage=lineage,
            persona_genes=_as_gene_list(data.get("persona_genes", [])),
            tool_genes=_as_gene_list(data.get("tool_genes", [])),
            workflow_genes=_as_gene_list(data.get("workflow_genes", [])),
            upgrade_genes=_as_gene_list(data.get("upgrade_genes", [])),
            est_system_tokens=_as_token_count(data.get("est_system_tokens", 0)))
        g.baseline_tokens_per_task = 8 * max(1, g.est_system_tokens) // 10
        return g

    def step_synthesize(self, g: Genome) -> Genome:
        self._log("正在把基因组合成为可运行的 Agent 提示词")
        prompt = SYNTHESIZE_PROMPT.replace("<<GENOME>>", g.to_json())
        data = self._chat_json(prompt, phase="distill:synthesize",
                               model=self.llm.model_main, max_tokens=3000)
        # system_prompt 若不是字符串（模型返回对象/数字/空），
        # 后续 _simulate_answer 会把它当 system 消息直接喂给模型，
        # 轻则蒸馏体拿到空人格、重则上游报 400。统一收敛为字符串。
        sp = data.get("system_prompt")
        g.system_prompt = sp if isinstance(sp, str) else (
            json.dumps(sp, ensure_ascii=False) if isinstance(sp, (dict, list))
            else "")
        g.tools = _as_gene_list(data.get("tools") or g.tool_genes)
        g.workflow = _as_gene_list(data.get("workflow"))
        g.est_system_tokens = len(g.system_prompt) // 4
        return g

    def step_gen_eval(self, g: Genome) -> list:
        self._log("正在生成用于对照评测的考题")
        prompt = (GEN_EVAL_PROMPT
                  .replace("<<MISSION>>", g.mission_one_liner)
                  .replace("<<ROLE>>", g.name))
        try:
            data = self._chat_json(prompt, phase="distill:gen_eval",
                                   model=self.llm.model_fast,
                                   max_tokens=4096)
            cases = [c for c in data.get("cases", [])
                     if isinstance(c, dict) and c.get("input")]
            self.question_source = "llm" if cases else "builtin"
            # 出题模型如实记录：题目由被测方（model_fast）自己出，
            # 天然更贴合它自己的表达方式与知识边界。
            if cases:
                self.question_model = str(
                    getattr(self.llm, "model_fast", "") or "")
        except Exception as e:                          # noqa: BLE001
            self._log(f"  自动出题失败（{user_error(e, 'distill.gen_eval')}），"
                      f"已改用内置的标准考题")
            cases = []
            self.question_source = "builtin"
        if not cases:
            self._log("  考题不可用，已改用内置的标准考题")
            self.question_source = "builtin"
            cases = [{"id": f"c{i+1}", "input": t,
                      "rubric": ["task_completion", "correctness",
                                 "efficiency", "polish"]}
                     for i, t in enumerate([
                         f"典型任务：{g.mission_one_liner}",
                         f"边界情况：{g.mission_one_liner}（输入不完整）",
                         "对抗输入：与使命无关的无关请求",
                         "压力测试：超长模糊的多部分请求"])]
            for c in cases:
                self._case_origin[c["input"]] = ("builtin", "builtin")
        else:
            # 模型自带 rubric 才算"标准自定"；没给 rubric 的用例到裁判
            # 那里拿到的是空标准，等价于内置默认，不该按自定从严处理。
            for c in cases:
                self._case_origin[c["input"]] = (
                    "llm", "llm" if c.get("rubric") else "builtin")
        # 用户指定的评测任务排在最前：这是用户最关心的场景，
        # 不该被自动生成的通用考题挤到后面甚至完全忽略。
        if self.task:
            self._log("  已加入用户指定的评测任务作为第 1 组用例")
            self._case_origin[self.task] = ("user", "user")
            cases = [{"id": "user_task", "input": self.task,
                      "rubric": ["task_completion", "correctness",
                                 "efficiency", "polish"]}] + [
                c for c in cases if c.get("input") != self.task]
        return cases

    # -- arena -----------------------------------------------------------
    def _tally_case_origins(self, cases: list) -> None:
        """按**真正参评**的用例统计题目/标准来源（arena_rounds 截断后）。

        抽成方法是因为这套"取最严一档"的判定若在测试桩里另写一份，
        桩与实现就会各说各话，判定口径随之分叉。
        """
        q_srcs, r_srcs = set(), set()
        for c in cases[:self.arena_rounds]:
            src, rub = self._case_origin.get(c.get("input"),
                                             ("builtin", "builtin"))
            q_srcs.add(src)
            r_srcs.add(rub)
        # 标准来源取最严的一档：只要有一条用了模型自定的尺子，
        # 整轮结论就带着这层偏差（分数不可拆分归因）。
        self.rubric_source = ("llm" if "llm" in r_srcs
                              else "user" if "user" in r_srcs
                              else "builtin")
        if "llm" not in q_srcs:
            self.question_model = ""   # 模型出的题没被评，等同没出

    def run_arena(self, g: Genome, cases: list,
                  generation: int) -> tuple:
        """Returns (distilled_avg, baseline_avg, judge_reasons, pair_log).

        双顺序检验（position-bias debiasing）
        -----------------------------------
        原实现是单顺序：A 恒为对照组、B 恒为蒸馏体。LLM judge 已知会
        系统性偏好固定位置的答案——若存在这种偏好，"蒸馏体更强"这个
        产品的核心结论就可能纯粹由摆放顺序产生，而不是被测出来的。

        这是最难发现的一类问题：它不报错、不崩溃，只会稳定地产出一个
        漂亮的假结论，而且样本越小越难察觉。

        做法：同一对答案评两次，交换 A/B 位置，取两侧各自的均值。
        位置偏好在平均时自然抵消；同时把它量化出来写进 pair_log，
        让"judge 是否偏心"本身成为可审计的数据，而不是藏在结论后面。

        两次判定若指向相反的胜负，不把它平均成一个胜率——那是把分歧
        伪装成确定。如实记为 disagreement，由上层决定是否采信。
        """
        d_scores, b_scores, reasons, pairs = [], [], [], []
        # 只统计**真正参评**的用例（上面按 arena_rounds 截断）：生成的题
        # 没被评到，就不该让结论为它背锅。用户给了 task 且 rounds=1 时，
        # 实际评的就是用户那道题，标准是人类硬编码的，结论应当可信。
        self._tally_case_origins(cases)
        for case in cases[:self.arena_rounds]:
            ans_d = self._simulate_answer(g, case["input"])
            ans_b = self._simulate_baseline(case["input"])
            base = (ARENA_PROMPT
                    .replace("<<TASK>>", case["input"])
                    .replace("<<RUBRIC>>", "; ".join(case.get("rubric", []))))
            # 顺序 1：A=对照组 B=蒸馏体；顺序 2：A=蒸馏体 B=对照组
            o1 = self._judge_once(base, ans_b, ans_d)
            o2 = self._judge_once(base, ans_d, ans_b)
            debiased = o1 is not None and o2 is not None
            counted = True            # 两侧全败时置 False：没评出来 ≠ 都得零分
            if debiased:
                # 对照组 = 顺序1的A、顺序2的B；蒸馏体 = 顺序1的B、顺序2的A
                s_base = (o1["a"] + o2["b"]) / 2.0
                s_dist = (o1["b"] + o2["a"]) / 2.0
                # 位置偏好：放在 A 位的平均得分 减 放在 B 位的平均得分
                pos_a = (o1["a"] + o2["a"]) / 2.0
                pos_b = (o1["b"] + o2["b"]) / 2.0
                bias = pos_a - pos_b
                # 两个顺序的 A/B 含义相反，映射必须跟着翻：
                #   顺序1（flip=False）A=对照组  B=蒸馏体
                #   顺序2（flip=True） A=蒸馏体  B=对照组
                #
                # 容易踩到的坑：原实现写成嵌套三元，**两个分支整体写反**。
                # 后果不是报错，而是产物里 winner 与得分恒相反——
                # 例如 蒸馏体 10.0 / 对照组 1.0，却记 winner="baseline"。
                # 前端把 winner 直接当"胜出方"上屏，用户看到的是一份
                # 自相矛盾的报告。更隐蔽的是：agree 判断两边同错、
                # 互相抵消，所以分歧检测完全看不出异常——只有拿 winner
                # 和分数对照时才会暴露。故拆成具名函数，不再用嵌套三元。
                def _side(w: str, flip: bool) -> str:
                    if w not in ("A", "B"):
                        return "tie"
                    if not flip:                      # 顺序1：A=对照组
                        return "baseline" if w == "A" else "distilled"
                    return "distilled" if w == "A" else "baseline"  # 顺序2：A=蒸馏体

                w1, w2 = _side(o1["winner"], False), _side(o2["winner"], True)
                agree = (w1 == w2)
                winner = w1 if agree else "disagreement"
                # 两次理由一致就只留一份——拼接会产生"双方持平 || 双方持平"
                # 这种重复文案，对用户是噪音。不一致才并列，保留分歧证据。
                reason = (o1["reason"] if o1["reason"] == o2["reason"]
                          else f"{o1['reason']} || {o2['reason']}".strip(" |"))
                # 污染标签同样要按 A/B 含义翻过来合并：
                #   顺序1 A=对照组 B=蒸馏体；顺序2 A=蒸馏体 B=对照组
                c_base = _uniq(_contam(o1, "A") + _contam(o2, "B"))
                c_dist = _uniq(_contam(o1, "B") + _contam(o2, "A"))
                self.arena_debiased += 1
            else:
                # 一侧判定失败：退回可用的那一侧，并如实标注未去偏。
                # 宁可带着"未去偏"标记给出结果，也不让单次失败拖垮整轮——
                # 但绝不能把单顺序结果当成去偏结果交出去。
                #
                # 两个顺序的 A/B 含义是**相反**的：
                #   顺序1 A=对照组 B=蒸馏体  →  a=对照组, b=蒸馏体
                #   顺序2 A=蒸馏体 B=对照组  →  a=蒸馏体, b=对照组
                # 所以退回哪一侧，映射就得跟着反过来。原实现统一取
                # (a, b) 当 (对照组, 蒸馏体)，于是只要顺序1失败、
                # 顺序2成功，两个分数就整体对调——例如：judge 判
                # 蒸馏体 9.0 / 对照组 1.0，产物里却记成
                # 蒸馏体 1.0 / 对照组 9.0，结论完全颠倒。
                # 这是产品最不能出错的地方（"更强"是核心主张）。
                if o1 is not None:
                    one = o1
                    s_base, s_dist = o1["a"], o1["b"]
                    c_base = _uniq(_contam(o1, "A"))
                    c_dist = _uniq(_contam(o1, "B"))
                elif o2 is not None:
                    one = o2
                    s_base, s_dist = o2["b"], o2["a"]
                    c_base = _uniq(_contam(o2, "B"))
                    c_dist = _uniq(_contam(o2, "A"))
                else:
                    one = {"a": 0.0, "b": 0.0, "winner": "tie", "reason": ""}
                    s_base, s_dist = 0.0, 0.0
                    c_base, c_dist = [], []
                    # 两个顺序都失败时**不得按 0:0 计入均值**：那会把
                    # "没评出来"伪装成"双方都得零分"，均值被双双拉低，
                    # 用户看到的分数里混进了根本没发生的评分。
                    counted = False
                bias, agree, winner = 0.0, False, "undetermined"
                reason = one["reason"]
                self._log("  一侧判定失败，本用例未能完成双顺序去偏，"
                          "结果按单顺序计（已在产物中标注）")
            if counted:
                d_scores.append(s_dist)
                b_scores.append(s_base)
            reasons.append(reason)
            pairs.append({"case": case["id"], "task": case["input"],
                          "answer_baseline": ans_b, "answer_distilled": ans_d,
                          "score_baseline": round(s_base, 2),
                          "score_distilled": round(s_dist, 2),
                          "judge_reason": reason,
                          "winner": winner,
                          "debiased": debiased,
                          "position_bias": round(bias, 2),
                          "orders_agree": agree,
                          "contamination": {
                              "distilled": [TAG_TEXT.get(t, t) for t in c_dist],
                              "baseline": [TAG_TEXT.get(t, t) for t in c_base],
                          },
                          "contaminated": bool(c_dist or c_base)})
            if c_dist or c_base:
                self.arena_contaminated += 1
                who = []
                if c_dist:
                    who.append("蒸馏体：" + "、".join(
                        TAG_TEXT.get(t, t) for t in c_dist))
                if c_base:
                    who.append("对照组：" + "、".join(
                        TAG_TEXT.get(t, t) for t in c_base))
                self._log(f"  用例 {case['id']} 的答案含评分操纵痕迹"
                          f"（{'；'.join(who)}），本条结论的可信度存疑")
            self._log(f"  第 {generation} 轮 · 用例 {case['id']}："
                      f"源 Agent {s_base:.1f} 分，蒸馏体 {s_dist:.1f} 分"
                      + ("" if debiased else "（未去偏）"))
        # 累加而不是赋值：contaminated 是跨代累加的，若这里每代重置，
        # 多代跑下来会出现"4/2 个用例"这种不可能的比例——分母只反映
        # 最后一代，分子却是全部代之和，用户据此判断不出结论有几成可信。
        self.arena_cases += len(pairs)
        return (sum(d_scores) / max(1, len(d_scores)),
                sum(b_scores) / max(1, len(b_scores)), reasons, pairs)

    def _judge_once(self, base_prompt: str, ans_a: str,
                    ans_b: str) -> dict | None:
        """评一次，返回该次评分；失败返回 None（由调用方决定是否降级）。

        判定失败不该拖垮整轮蒸馏——双顺序把 judge 调用次数翻倍，
        失败概率随之上升，若沿用"一次失败即崩溃"，等于用可靠性换
        正确性，这个交换在核心结论链上不划算。

        两次答案在进提示词前**降级为数据**：

        被测方有可能在答案里塞自我标榜或打分要求，试图影响裁判。
        这个产品在结构上就暴露于此类风险——题目由 LLM 出、答案由
        LLM 写、评分由同源 LLM 做，出题/作答/裁判在同一条利益链上。
        若被测方答案中含自我标榜，裁判可能顺从给出悬殊分数，
        产物因此必须标记评分可能受操纵。

        这类污染**不能靠双顺序去偏解决**：位置去偏只抵消"跟着位号走"
        的操纵（提 ANSWER A/B 的，换位置就反向抵消），而真实的
        reward hacking 跟着内容走，两个顺序里都同时生效。

        只标记、不剔除：静默剔除会把"样本不足"藏起来，而用户需要的
        恰恰是知道这轮结论有几条是被污染的。
        """
        tag_a = scan_judge_manipulation(ans_a)
        tag_b = scan_judge_manipulation(ans_b)
        # source 刻意**不带位号**：两个顺序里 A 位放的是不同答案，若把
        # "ANSWER A"/"ANSWER B" 写进分隔标记，同一份内容在两个顺序里会得到
        # 不同的包裹文本，于是任何"按内容识别答案"的下游逻辑都认不出来——
        # 更糟的是这等于**新引入了一层位置依赖**：刚修好位置偏差，又添一个。
        # 位号由 ARENA_提示词 自己的 "ANSWER A:" 标题承担，分隔标记只说性质。
        prompt = (base_prompt
                  .replace("<<ANSWER_A>>",
                           wrap_candidate(ans_a, "候选答案", tag_a))
                  .replace("<<ANSWER_B>>",
                           wrap_candidate(ans_b, "候选答案", tag_b)))
        try:
            data = self._chat_json(prompt, phase="arena:judge",
                                   model=self.llm.model_judge)
        except Exception as e:                      # noqa: BLE001
            self._log(f"  判定失败（已降级处理）："
                      f"{user_error(e, 'distill.run_arena')}")
            return None
        scores = data.get("scores")
        scores = scores if isinstance(scores, dict) else {}
        w = data.get("winner", "tie")
        return {"a": _avg_scores(scores.get("A", {})),
                "b": _avg_scores(scores.get("B", {})),
                "contamination": {"A": tag_a, "B": tag_b},
                "winner": w if w in ("A", "B", "tie") else "tie",
                "reason": str(data.get("reason", ""))}

    def _simulate_answer(self, g: Genome, task: str) -> str:
        self.bank.charge_estimate("arena:answer", self.llm.model_fast, 600)
        r = self.llm.chat(g.system_prompt, task, model=self.llm.model_fast,
                          max_tokens=1200)
        self.bank.charge("arena:answer", r.model, r.prompt_tokens,
                         r.completion_tokens, note=g.name)
        return r.text

    def _simulate_baseline(self, task: str) -> str:
        """对照组作答。

        关键变更：不再使用编造的弱提示（"Answer the task thoroughly"），
        改用源 agent 的原始 提示词（Baseline.system_提示词）。
        否则"蒸馏体更强"是实验设计保证的，而非被测出的。
        """
        if self.baseline is None:
            # 直接调用 run_arena() 而未经 distill() 的场景：
            # 没有源材料可推导对照组，只能兜底为 简化对照 —— 明确不可比，
            # 但不阻断流程（调用方可能只想要一次作答对比）。
            self.baseline = Baseline(
                kind="naive",
                system_prompt="You are an assistant. "
                              "Complete the task as instructed.",
                note="未经 distill() 初始化，无源材料可推导对照组（不可比）",
            )
            self._log("  对照组未初始化，已改用简化对照（此轮不可比）")
        self.bank.charge_estimate("arena:answer", self.llm.model_fast, 500)
        r = self.llm.chat(self.baseline.system_prompt, task,
                          model=self.llm.model_fast, max_tokens=1500)
        self.bank.charge("arena:answer", r.model, r.prompt_tokens,
                         r.completion_tokens, note="baseline")
        return r.text

    # -- evolution ---------------------------------------------------------
    def _select_evolution_evidence(self, pairs):
        """挑出可用于驱动进化的可信证据。

        返回 (focus_pair, gap_text, trusted_count)；无可信证据时返回 None。

        可信 = 该对完成了双顺序去偏 **且** 两侧答案均无评分操纵痕迹。
        两个条件缺一不可：未去偏意味着分数里混着摆放顺序造成的偏差，
        被污染意味着分数可能是对裁判施加影响的结果——两者都会让
        "差距在哪"这个判断失真，而进化的全部工作正是依据这个判断
        改写基因，并让它在后续代际继续放大。
        """
        trusted = [p for p in pairs
                   if p.get("debiased") and not p.get("contaminated")]
        if not trusted:
            return None
        # 败因优先：蒸馏体输得越多的对越能指出差距在哪
        losers = sorted(
            trusted,
            key=lambda p: (p.get("score_distilled", 0)
                           - p.get("score_baseline", 0)))
        focus = losers[0]
        # 汇总而非取单条：单条理由可能只是个例，三条取其交集更稳
        gap = "；".join(dict.fromkeys(
            (p.get("judge_reason") or "").strip()
            for p in losers[:3]
            if (p.get("judge_reason") or "").strip()))
        return focus, gap, len(trusted)

    def evolve_step(self, g: Genome, task: str, reason: str,
                    ans_a: str, ans_b: str) -> Genome:
        # 与 arena 同一条防线，但后果更远：arena 的污染只影响一次得分，
        # 这里的污染会**遗传进 genome**——进化是迭代的，被操纵的差距
        # 判断会变成下一代的基因，并在后续轮次里继续放大。
        tag_a = scan_judge_manipulation(ans_a[:1500])
        tag_b = scan_judge_manipulation(ans_b[:1500])
        prompt = (CRITIQUE_PROMPT
                  .replace("<<TASK>>", task)
                  .replace("<<REASON>>", reason)
                  .replace("<<ANSWER_A>>",
                           wrap_candidate(ans_a[:1500], "候选答案", tag_a))
                  .replace("<<ANSWER_B>>",
                           wrap_candidate(ans_b[:1500], "候选答案", tag_b)))
        data = self._chat_json(prompt, phase="evolve:critique",
                               model=self.llm.model_fast)
        deltas = _as_gene_list(data.get("deltas"))
        g.upgrade_genes.extend(deltas)
        g.arena_history.append({"deltas": deltas})
        return self.step_synthesize(g)   # recompile 提示词 with new genes

    # -- 消融归因 ----------------------------------------------------------
    # 为什么需要：进化只知道"分数变了"，不知道"是哪条基因导致的"。
    # 于是下一次进化仍在盲改——批评意见一次覆盖全部基因，有害基因会被
    # 反复加回来。归因把"该改哪一条"变成一个可回答的问题。
    #
    # 口径必须与主 arena 完全一致（双顺序去偏 + 污染标记），否则消融
    # 结论与主结论不可比——拿两把不同的尺子量出来的差值没有意义。

    def _score_genome(self, g, cases, baseline_cache=None) -> dict:
        """在同一份冻结用例上给一个 genome 打分。

        对照组答案跨变体不变，故缓存：消融要跑 N 个变体，
        每个变体重算一次基线等于白烧一倍预算。
        """
        d_scores, debiased, contaminated = [], 0, 0
        for case in cases[:self.arena_rounds]:
            key = str(case.get("input", "") or "")
            ans_d = self._simulate_answer(g, key)
            if baseline_cache is not None and key in baseline_cache:
                ans_b = baseline_cache[key]
            else:
                ans_b = self._simulate_baseline(key)
                if baseline_cache is not None:
                    baseline_cache[key] = ans_b
            base = (ARENA_PROMPT
                    .replace("<<TASK>>", key)
                    .replace("<<RUBRIC>>", "; ".join(case.get("rubric", []))))
            o1 = self._judge_once(base, ans_b, ans_d)   # A=对照 B=蒸馏
            o2 = self._judge_once(base, ans_d, ans_b)   # A=蒸馏 B=对照
            if o1 is None or o2 is None:
                continue                                # 没评出来 ≠ 得零分
            debiased += 1
            # 蒸馏体的污染标签：顺序1 在 B 位，顺序2 在 A 位
            if ((o1.get("contamination") or {}).get("B")
                    or (o2.get("contamination") or {}).get("A")):
                contaminated += 1
            d_scores.append((o1["b"] + o2["a"]) / 2.0)
        return {"score": (sum(d_scores) / len(d_scores)) if d_scores else None,
                "debiased": debiased, "contaminated": contaminated}

    @staticmethod
    def _ablation_targets(g, max_variants: int) -> list:
        """可消融的基因清单。upgrade 优先：它们是进化产物，最可能有害。"""
        out = []
        for kind in ("upgrade_genes", "persona_genes", "workflow_genes"):
            for i, gene in enumerate(getattr(g, kind) or []):
                if gene:
                    out.append((kind, i, gene))
        return out[:max_variants]

    def ablate_genes(self, g, cases, max_variants: int = ABLATION_MAX_VARIANTS,
                     min_gain: float = ABLATION_MIN_GAIN) -> dict:
        """逐条去掉基因重测，判断每条是有益、有害还是中性。

        方向极易写反，这里明确一次：
          delta = 变体分 - 完整分
          delta > 0 → 去掉它反而变好 → 该基因**有害**
          delta < 0 → 去掉它变差     → 该基因**有益**

        只报告、不自动删除：样本量通常只有 1~6 条用例，
        单次消融的结论是统计性的，自动 prune 可能删掉真正有用的基因。
        """
        if not cases:
            return {"enabled": True, "variants": [], "note": "无评测用例，未做消融"}
        cache: dict = {}
        try:
            base = self._score_genome(g, cases, cache)
        except BudgetExceeded:
            return {"enabled": True, "aborted": True,
                    "note": "预算不足，未做消融"}
        if base["score"] is None:
            return {"enabled": True, "aborted": True,
                    "note": "完整体未评出分数，消融无从比较"}
        targets = self._ablation_targets(g, max_variants)
        if not targets:
            return {"enabled": True, "variants": [],
                    "note": "没有可消融的基因"}
        results = []
        for kind, idx, gene in targets:
            v = copy.deepcopy(g)
            setattr(v, kind,
                    [x for j, x in enumerate(getattr(g, kind) or [])
                     if j != idx])
            try:
                v = self.step_synthesize(v)   # 基因变了必须重新合成才有意义
                sv = self._score_genome(v, cases, cache)
            except BudgetExceeded:
                # 消融是附加诊断，不该把整轮蒸馏拖垮
                return {"enabled": True, "aborted": True,
                        "note": f"预算在第 {len(results) + 1} 个变体处耗尽",
                        "baseline_score": round(base["score"], 3),
                        "variants": results}
            except Exception as e:                          # noqa: BLE001
                results.append({"kind": kind, "gene": str(gene)[:120],
                                "error": user_error(e, "ablate")})
                continue
            if sv["score"] is None:
                results.append({"kind": kind, "gene": str(gene)[:120],
                                "error": "该变体未评出分数"})
                continue
            delta = sv["score"] - base["score"]
            if delta > min_gain:
                verdict = "harmful"
            elif delta < -min_gain:
                verdict = "beneficial"
            else:
                verdict = "neutral"
            results.append({"kind": kind, "gene": str(gene)[:120],
                            "score": round(sv["score"], 3),
                            "delta": round(delta, 3), "verdict": verdict,
                            "debiased": sv["debiased"],
                            "contaminated": sv["contaminated"]})
        comparable = bool(results) and all(
            (v.get("debiased") or 0) > 0 and not v.get("contaminated")
            for v in results if "error" not in v)
        # 分辨力：所有变体得分一模一样，说明"去掉哪条基因"根本没造成差异
        # ——变体之间不可区分，归因结论自然无从谈起。
        #
        # （mock 下真实出现）：6 个变体的 delta 全是 -1.92。看着像
        # "每条基因贡献相同且都有益"，实际是去掉任意一条后合成出的
        # 提示词退化成了同一个东西。若不标记，用户会照着一份分辨力
        # 为零的报告去删基因。
        scored = [v["score"] for v in results if "error" not in v]
        discriminative = len(set(scored)) > 1
        out = {"enabled": True, "baseline_score": round(base["score"], 3),
               "min_gain": min_gain, "variants": results,
               "comparable": comparable and discriminative,
               "discriminative": discriminative,
               "truncated": len(self._ablation_targets(g, 10 ** 9)) > len(results)}
        if not discriminative:
            out["note"] = ("各变体得分完全相同，消融未产生区分——"
                           "去掉哪条基因都没造成差异，当前归因不可采信")
        return out

    @staticmethod
    def eval_set_fingerprint(cases: list) -> str:
        """评测集指纹：跨版本比对的唯一凭据。

        只对 **input + rubric** 取指纹，不含 id：id 是编号，换一套编号
        不代表换了一套题；反之题目改了一个字，分数就不该再可比。

        顺序无关：用例顺序不影响"是不是同一套题"。
        """
        if not cases:
            return ""
        items = sorted(
            (str(c.get("input", "") or "").strip(),
             tuple(str(x) for x in (c.get("rubric") or [])))
            for c in cases if isinstance(c, dict))
        if not items:
            return ""
        blob = json.dumps(items, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # -- 跨版本比对 --------------------------------------------------------

    @staticmethod
    def compare_reports(prev: dict, curr: dict) -> dict:
        """跨版本比对：本版比上一版本强了多少、依据哪套题。

        最难发现的一类错误不是算错，而是**把不可比的两个数相减**。
        题不同、对照组不可比、任一侧结论不成立——这三种情况下 delta
        都是一个看起来精确、实则无意义的数字。所以这里一律返回
        comparable=False 并说明原因，**不给 delta**。
        """
        def _get(d, k, default=None):
            return (d or {}).get(k, default)

        out = {
            "comparable": False,
            "reason": "",
            "delta": None,
            "prev_score": _get(prev, "final_score"),
            "curr_score": _get(curr, "final_score"),
            "fingerprint": _get(curr, "eval_set_fingerprint", "") or "",
            "prev_claim_valid": bool(_get(prev, "claim_valid", False)),
            "curr_claim_valid": bool(_get(curr, "claim_valid", False)),
        }
        if not isinstance(prev, dict) or not isinstance(curr, dict):
            out["reason"] = "缺少可供比对的历史报告"
            return out
        pf = _get(prev, "eval_set_fingerprint", "") or ""
        cf = _get(curr, "eval_set_fingerprint", "") or ""
        if not pf or not cf:
            out["reason"] = "历史报告缺少评测集指纹，无法确认用的是同一套题"
            return out
        if pf != cf:
            out["reason"] = ("两次用的是不同的评测集（指纹 "
                             f"{pf[:8]} vs {cf[:8]}），分数不可比")
            return out
        if not _get(prev, "baseline_comparable", False):
            out["reason"] = "上一版本的对照组不可比，差值不构成证据"
            return out
        if not _get(curr, "baseline_comparable", False):
            out["reason"] = "本版的对照组不可比，差值不构成证据"
            return out
        if not out["prev_claim_valid"] or not out["curr_claim_valid"]:
            # 只说"不成立"，用户不知道卡在哪、更不知道怎么才能拿到差值，
            # 于是反复重试——而重试一百次结果都一样。必须给出可行动指引。
            out["reason"] = (
                "有一侧的结论本身不成立，差值不构成证据。"
                "常见原因是作答与评审用的是同一套本地演示模型（自我评分），"
                "或考题由被测方自定。请在设置里配置真实的作答模型与不同的"
                "评审模型后重新蒸馏，再比对")
            return out
        try:
            delta = float(_get(curr, "final_score", 0)) - \
                float(_get(prev, "final_score", 0))
        except (TypeError, ValueError):
            out["reason"] = "分数格式异常，无法计算差值"
            return out
        out["comparable"] = True
        out["delta"] = round(delta, 2)
        return out

    @staticmethod
    def _write_eval_set(output_dir: str, cases: list) -> str | None:
        """把本版使用的用例冻结成文件，供下一轮复用。

        只存 input/id/rubric 三键：不落内部字段，
        避免与使用者无关的信息进入用户可读文件。
        """
        try:
            payload = {"version": 1,
                       "cases": [{"id": c.get("id", ""),
                                  "input": c.get("input", ""),
                                  "rubric": list(c.get("rubric", []) or [])}
                                 for c in cases]}
            p = os.path.join(output_dir, "eval_set.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return p
        except OSError:
            # 落盘失败不该让整轮蒸馏崩掉：评测集是复用件，不是产物本体。
            return None

    # -- 自进化骨架：冻结评测集 --------------------------------------------

    # 冻结评测集是"可评测"这条腿的落点：每代在同一套题上打分，且**跨次
    # 运行**沿用同一套题。否则题每次重新生成，分数不可比，进化既无法
    # 判断改善，也无法跨版本比较。
    EVAL_SET_MAX_CASES = 100
    EVAL_SET_MAX_INPUT_CHARS = 4000
    EVAL_SET_MAX_RUBRIC_ITEMS = 20

    @classmethod
    def load_eval_set(cls, path: str) -> list:
        """从文件加载冻结评测集。

        为什么必须显式校验而不是直接信任：评测集是要被反复复用的输入，
        一条畸形用例会让每一轮、每一代都在同一个地方失败，而用户在
        产物里只看到"未评出来"。错误必须在**加载时**就暴露。
        """
        from omegaforge.core.errors import UserError
        if not path or not str(path).strip():
            raise UserError("请指定评测集文件路径")
        p = pathlib.Path(os.path.expanduser(str(path)))
        if not p.exists():
            raise UserError(f"评测集文件不存在：{p.name}")
        if not p.is_file():
            raise UserError("评测集路径是一个目录，请指定文件")
        try:
            raw = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise UserError("评测集不是 UTF-8 文本，无法读取")
        try:
            data = json.loads(raw)
        except ValueError:
            raise UserError("评测集不是合法 JSON")
        if isinstance(data, dict):
            if "cases" not in data:
                raise UserError("评测集缺少用例列表")
            data = data["cases"]
        return cls._normalize_eval_set(data)

    @classmethod
    def _normalize_eval_set(cls, data) -> list:
        """校验并归一化评测集（文件加载与 HTTP 入参共用的唯一入口）。

        共用是刻意的：分成两套校验必然出现"某个入口漏了某条"，
        而评测集是要被反复复用的输入，漏一条就等于每一轮都带着它跑。
        """
        from omegaforge.core.errors import UserError
        if not isinstance(data, list):
            raise UserError("评测集内容应为一组用例")
        if not data:
            raise UserError("评测集为空，至少需要 1 条用例")
        if len(data) > cls.EVAL_SET_MAX_CASES:
            raise UserError(
                f"评测集有 {len(data)} 条用例，超过上限 "
                f"{cls.EVAL_SET_MAX_CASES}，请拆分后再用")
        cases = []
        for i, c in enumerate(data):
            if not isinstance(c, dict):
                raise UserError(f"评测集第 {i+1} 条的格式不正确")
            text = str(c.get("input", "") or "").strip()
            if not text:
                raise UserError(f"评测集第 {i+1} 条缺少题目内容")
            if len(text) > cls.EVAL_SET_MAX_INPUT_CHARS:
                raise UserError(
                    f"评测集第 {i+1} 条的题目内容过长（{len(text)} 个字符，"
                    f"上限 {cls.EVAL_SET_MAX_INPUT_CHARS}）")
            rub = c.get("rubric", [])
            if rub is None:
                rub = []
            if not isinstance(rub, list):
                raise UserError(f"评测集第 {i+1} 条的评分标准应为一组条目")
            if len(rub) > cls.EVAL_SET_MAX_RUBRIC_ITEMS:
                raise UserError(
                    f"评测集第 {i+1} 条的评分标准条目过多"
                    f"（上限 {cls.EVAL_SET_MAX_RUBRIC_ITEMS}）")
            cases.append({
                "id": str(c.get("id") or f"c{i+1}"),
                "input": text,
                "rubric": [str(x) for x in rub],
            })
        return cases

    # -- 自进化骨架：快照 / 择优晋升 / 回滚 --------------------------------

    # 低于此改善幅度视为"没有改善"。进化不是免费的：它要花 token、要花
    # 一代评测，而纯内在纠错在缺少可对照的标准答案时净收益可能为负
    # （把正确答案改错的比例高于修复错误答案的比例）。
    # 所以"差不多"必须按"没进步"处理——这是自进化唯一诚实的收敛条件。
    EVOLUTION_MIN_GAIN = 0.05

    @staticmethod
    def _snapshot(g: Genome, gen: int, score: float) -> dict:
        """代际快照：可版本化的最小单元。

        存 JSON 而不是对象引用——引用会被后续 evolve_step 就地改写，
        快照就变成了"事后的自己"，等于没存。
        """
        return {"gen": gen, "score": float(score), "json": g.to_json()}

    @staticmethod
    def _restore(snap: dict) -> Genome:
        """从快照恢复基因组。快照残缺时抛 UserError，绝不静默返回空的 g。"""
        raw = (snap or {}).get("json")
        if not raw:
            raise UserError("缺少历史记录，无法回到最佳结果")
        return Genome.from_json(raw)

    @staticmethod
    def _select_best_generation(prev_best: dict | None, gen: int,
                                score: float, genome_json: str,
                                min_gain: float = None) -> tuple:
        """择优晋升判定。返回 (best, improved)。

        improved=False 表示本代没有产生有意义的改善，调用方应**回滚**
        到 best 那代的基因。

        为什么必须显式回滚：进化是**就地改写** upgrade_genes 的，不回滚
        的话，越跑越差的那几代已经写进了 genome，而产物保存的是最后一代
        ——用户拿到的可能恰好是历代最差的基因组，却被告知"已进化 N 代"。
        """
        if min_gain is None:
            min_gain = DistillEngine.EVOLUTION_MIN_GAIN
        cur = {"gen": gen, "score": float(score), "json": genome_json}
        if prev_best is None:
            return cur, True
        if float(score) > float(prev_best.get("score", 0.0)) + float(min_gain):
            return cur, True
        return prev_best, False

    # -- orchestration -------------------------------------------------------
    def _trust_note(self, report: DistillReport) -> str:
        """结论说明：按"哪一条不成立"给出唯一且可执行的原因。

        抽成方法而不是留在装配处内联，是因为内联无法单独调用——只能
        构造报告对象再断言字段，而字段的默认值就是空串，于是
        "分母为 0 时不给比例"这条断言恒真：它测的是默认值，不是装配。

        分支顺序即优先级：对照组不可比 > 污染 > 自证 > 考题自定 >
        未去偏 > 零用例。前一条成立时不说后一条，否则用户照着做无效
        修复，还以为已经处理过了。
        """
        if not report.baseline_comparable:
            return (
                f"当前对照组是简化对照，"
                f"并非源 Agent 的真实提示词，两者不具可比性——"
                f"赢一个不存在的对手不构成「更强」。"
                f"若要得到可采信的结论，请提供源 Agent 原始系统提示词 "
                f"作为对照，或在评测任务里指定对照对象。")
        elif self.arena_contaminated:
            return (
                f"{self.arena_contaminated}/{self.arena_cases} 个用例的答案"
                f"含评分操纵痕迹（自我评价、索要分数或对裁判下指令），"
                f"这些得分可能不是被测出来的。"
                f"本次胜负不足以支撑「更强」结论，请检查被测方提示词。")
        elif report.self_certified:
            # 与上面两者分开说：污染是"证据可疑"，未去偏是"证据偏弱"，
            # 由同一模型作答与评分是**裁判与被测方同源**——模型在给自己的输出打分。
            # 这不是抽样问题，多跑几轮也不会改善，只能换裁判模型。
            return (
                f"裁判与作答为同一模型（{report.judge_model or '未知'}），"
                f"评分可能受自偏好影响：模型倾向于给自己或同族模型的输出"
                f"打高分。这种偏差作用于每一个用例，不会随样本增加抵消。"
                f"本次胜负不足以支撑「更强」结论；若要得到可采信的结论，"
                f"请把裁判配置为另一个模型（OMEGAFORGE_MODEL_JUDGE）。")
        elif report.exam_self_authored:
            # 与由同一模型作答与评分分开说：由同一模型作答与评分是"谁来判"有问题，这里是"考什么、按什么
            # 标准判"有问题——**换裁判模型修不掉**。用户若已换了裁判，
            # 会误以为结论独立了，所以必须给出独立的可执行路径。
            return (
                f"考题与评分标准由被测方自己生成"
                f"（出题模型 {report.question_model or '未知'}），"
                f"裁判再独立也是按被测方定的尺子打分——换裁判模型无法消除"
                f"这层偏差。若要得到可采信的结论，请在评测任务中填入你"
                f"自己的考题，并让评测轮次只覆盖它，标准即为人工给定。")
        elif self.arena_debiased < self.arena_cases:
            # 与污染分开说：污染是"证据可疑"，这里是"证据偏弱"。
            # 位置偏差只有双顺序都成功才抵消得掉，未去偏的用例仍可能
            # 只是因为摆在前头才赢——而摆放顺序恰恰是我们自己定的。
            return (
                f"{self.arena_cases - self.arena_debiased}/{self.arena_cases}"
                f" 个用例未能完成双顺序去偏（其中一侧判定失败），"
                f"这些得分仍可能受摆放位置影响。"
                f"本次胜负不足以支撑「更强」结论，可稍后重试。")
        elif self.arena_cases <= 0:
            # "至少评估过一个用例"这一条同样必须有对应分支。结论判定不该
            # 依赖"恰好走不到"来保证——真走到时，用户拿到的是一个"结论不
            # 成立且无原因"的产物。
            return (
                "当前没有完成任何用例的对照评测，「更强」没有支撑："
                "请检查预算是否足以跑完至少一轮对照，或稍后重试。")
        return report.trust_note

    def distill(self, source: str,
                output_dir: str = "output", task: str = "",
                ablate: bool = False, baseline_prompt: str = "") -> tuple:
        # `os.makedirs(output_dir)` 裸调用，两类填错都伪装成
        # 系统故障——
        #   --out ""            → FileNotFoundError → 未找到对应记录
        #   --out /etc/hostname → NotADirectoryError → 操作失败，请稍后重试
        # 后者尤其离谱：用户只是把输出目录填成了一个已存在的文件，
        # 却被报成 500 级故障，且 CLI 与服务端是同一条路径。
        # 在这里收口（而非 CLI 单独判一次）：distill() 是 CLI / server 共用的
        # 唯一起点，否则又会出现"某个入口忘了接"的老问题。
        if not output_dir or not str(output_dir).strip():
            raise UserError("请指定产物输出目录")
        if os.path.exists(output_dir) and not os.path.isdir(output_dir):
            raise UserError(
                "输出目录填写的是一个已存在的文件，请改用一个目录路径")
        os.makedirs(output_dir, exist_ok=True)
        if task:
            self.task = task.strip()
        # 与 task 同口径：入口侧传入优先于构造时传入，避免"某个入口忘了接"
        if baseline_prompt:
            self.baseline_prompt = baseline_prompt.strip()
        self._enter("ingest")
        loader = SourceAgentLoader()
        sig = loader.load(source)
        # 对照组必须在流水线开始前确定：它定义了"更强"是和谁比。
        # baseline_提示词 必须转发进来——本文档的 可信度说明 让用户
        # "提供源 Agent 原始 系统提示词"，若不转发，那句指引就是死路。
        if self.baseline is None:
            self.baseline = build_baseline(
                sig, provided_prompt=self.baseline_prompt)
        self._log(f"对照组：{self.baseline.label}（"
                  f"{len(self.baseline.system_prompt)} 字符）")
        if not self.baseline.comparable:
            self._log("  对照组不可比，当前胜负不构成“更强”的证据")
        self._enter("extract")
        spec = self.step_extract(sig)
        self._enter("compress")
        g = self.step_compress(spec, lineage=f"distill-of:{sig.fingerprint}",
                               fingerprint=sig.fingerprint)
        self._enter("synthesize")
        g = self.step_synthesize(g)
        self._enter("gen_eval")
        report = DistillReport(
            source_signals=sig.summary(),
            baseline_kind=self.baseline.kind,
            baseline_comparable=self.baseline.comparable,
            baseline_note=self.baseline.note,
        )
        # 冻结评测集：优先沿用既有的一套题。
        # 题每轮重新生成，于是"本版比上一版本强"在
        # 跨次运行上根本无从比较——分数不可比，进化既判断不了改善，
        # 用户也对比不了版本。同时题由被测方自己出（出题侧由同一模型作答与评分），
        # 换裁判模型也切不断这层偏差。
        if self.eval_set:
            cases = self.eval_set
            self.question_source = "user"
            self.question_model = ""
            for c in cases:
                self._case_origin[c.get("input", "")] = (
                    "user", "user" if c.get("rubric") else "builtin")
            report.eval_set_source = "provided"
            self._log(f"沿用既有评测集 {len(cases)} 条"
                      f"（题目与标准由使用者提供）")
        else:
            cases = self.step_gen_eval(g)
            report.eval_set_source = "generated"
        # 落盘，供下一轮复用：不写下来，跨次比较就无从谈起。
        if self._enter("freeze_eval") is None:
            self._write_eval_set(output_dir, cases)
            report.eval_set_cases = len(cases)
        # 指纹在**沿用与否都要算**：本版实际用的题才是比对的依据。
        report.eval_set_fingerprint = self.eval_set_fingerprint(cases)
        all_pairs: list = []
        best: dict | None = None      # 最佳一代的快照（自进化回滚的基准）

        for gen in range(1, self.max_generations + 1):
            report.generation = gen
            report.generations_run = gen
            self._enter("arena")
            d_avg, b_avg, reasons, pairs = self.run_arena(g, cases, gen)
            all_pairs.extend(pairs)
            g.arena_generation = gen
            g.arena_history.append({
                "gen": gen, "distilled": round(d_avg, 2),
                "baseline": round(b_avg, 2)})
            # 自进化：每代末择优晋升，未改善则**立即回滚**。
            # 进化就地改写 upgrade_genes 且从不回滚，
            # 于是"越改越差"的几代已经写进 genome，而产物保存的是
            # 最后一代——用户拿到的可能恰好是历代最差的基因组，却被告知
            # "已进化 N 代"。arena_best_score 赋的也是 final（最后一代）
            # 而不是历届最大值，字段名与语义不符，进一步掩盖了这件事。
            best, improved = self._select_best_generation(
                best, gen, d_avg, g.to_json())
            if not improved:
                g = self._restore(best)
                report.rolled_back = True
                report.evolution_notes.append(
                    f"第 {gen} 代得分 {d_avg:.2f} 未超过最佳第 "
                    f"{best['gen']} 代（{best['score']:.2f}），"
                    f"已回滚到最佳一代")
                self._log(f"第 {gen} 代：得分未改善，回滚至第 "
                          f"{best['gen']} 代（{best['score']:.2f}）")
            report.final_score, report.baseline_score = d_avg, b_avg
            # 评审理由必须在**所有**分支写入。
            # 例如：原有写法只在 loss 分支赋 judge_reasons，于是"蒸馏体胜出"
            # 这个最重要的场景下，报告里的理由列表是空的 ——
            # 用户拿到"更强"的结论，却看不到任何一条支撑它的评审意见，
            # 结论因此不可复核。恰恰是赢了才最需要证据。
            report.judge_reasons = reasons
            if d_avg > b_avg + 0.25:
                report.verdict = "win"
                break
            if b_avg - 0.25 <= d_avg <= b_avg + 0.25:
                report.verdict = "tie"
                break
            report.verdict = "loss"
            self._log(f"第 {gen} 代：蒸馏体 {d_avg:.2f} 分，"
                      f"对照 {b_avg:.2f} 分，正在继续进化")
            # 进化的方向只能来自可信证据。
            # evolve_step 取 pairs[0].judge_reason 作为唯一
            # 依据，且完全不看 debiased / contaminated。于是——
            #   · 去偏整体失败（0/N 完成）时分数仍带位置偏差，却照样驱动进化；
            #   · 答案含评分操纵痕迹的对，其理由照样被写进基因；
            #   · 只取第一条，其余所有对的证据被丢弃。
            # 后果不是"进化慢一点"：不可信的判断会被编译成基因并在后续
            # 代际继续放大。arena 的污染只影响一次得分，evolve 的污染会
            # 遗传——这是全流水线中后果最远的一处调用链路。
            # 且本版 judge 与作答默认同源（由同一模型作答与评分），纯内在纠错在缺少
            # 可对照的标准答案时净收益可能为负（改错率高于修复率），
            # 因此"没有可信证据就不进化"是唯一诚实的选择。
            ev = self._select_evolution_evidence(pairs)
            if ev is None:
                report.evolution_notes.append(
                    f"第 {gen} 代：无可信证据（去偏完成且未被污染的用例 "
                    f"0/{len(pairs)}），本代未进化，保留上一代基因")
                self._log(f"第 {gen} 代：无可信证据支撑进化方向，"
                          f"保留上一代基因（0/{len(pairs)} 条可信）")
            else:
                focus, gap, n_trusted = ev
                self._enter("evolve")
                g = self.evolve_step(
                    g, focus.get("task", "generic task"),
                    gap or "unknown gap",
                    focus.get("answer_baseline", "n/a"),
                    focus.get("answer_distilled", "n/a"))
                report.evolution_notes.append(
                    f"第 {gen} 代：应用了 {len(g.upgrade_genes)} 项增强"
                    f"（依据 {n_trusted}/{len(pairs)} 条可信证据）")
            if self.bank.remaining < 20_000:
                report.verdict = "budget-exhausted"
                self._log("预算已接近上限，提前结束进化")
                break

        self._enter("finalize")
        report.arena_cases = self.arena_cases
        report.contaminated_cases = self.arena_contaminated
        report.debiased_cases = self.arena_debiased
        # 裁判链必须落进产物：不写下来，"更强"就是无源之水。
        # 作答侧两个模型都取自 llm 配置；用 getattr 兜底是因为 llm 可被
        # 替换为第三方客户端，缺属性时记空串（产物侧按 fail-closed 处理），
        # 但绝不能因为取属性而让整轮崩溃。
        report.answer_model = str(getattr(self.llm, "model_fast", "") or "")
        report.judge_model = str(getattr(self.llm, "model_judge", "") or "")
        report.question_source = self.question_source or "unknown"
        # 出题侧闭环必须落进产物：不写下来，用户换了个第三方裁判就以为
        # 结论独立了，而考题与评分标准其实仍是被测方自己定的。
        report.question_model = self.question_model or ""
        report.rubric_source = self.rubric_source or ""
        if report.exam_self_authored and report.arena_cases:
            self._log("  注意：考题与评分标准由被测方自己生成，"
                      "换裁判模型也绕不开，当前结论不作为「更强」的依据")
        if report.self_certified and report.arena_cases:
            self._log(f"  注意：裁判与作答为同一模型"
                      f"（{report.judge_model or '未知'}），"
                      f"当前结论属自我评分，不作为「更强」的依据")
        # 对照组不可比必须排在**最前面**：它是"有没有可比对象"的问题，
        # 比"谁来判、按什么标准判"更根本——换裁判模型、换考题都修不好
        # 一个不存在的对照组。默认配置下「由同一模型作答与评分」与「不可比」常常
        # 同时成立，若把由同一模型作答与评分原因排在前面，用户换了裁判模型之后
        # 结论是否成立 仍为假，正是本文档批评过的"给错原因：
        # 用户照着做无效修复，还以为已经处理过了"。
        #
        # 结论是否成立由多个条件共同决定，而给出的原因必须覆盖全部条件。
        # 遗漏任何一条，都会出现"结论被判不成立却不给原因"——只读
        # 对照组说明 之外的消费方拿到的是一个没有理由的否定结论。
        #
        # 这一支对应"源材料里提取不到可用的系统提示词、对照组改用简化
        # 对照"，是最容易触发的一条。
        report.trust_note = self._trust_note(report)
        # 交付的必须是**最佳一代**，而不是最后一代。
        # 无论从哪个分支退出（win / tie / 预算耗尽 / 代际用尽），
        # 都要在这里统一收口——分散在各分支里必然有某处漏掉。
        if best is not None:
            report.best_generation = best["gen"]
            report.final_score = float(best["score"])
            if report.best_generation != g.arena_generation or report.rolled_back:
                g = self._restore(best)
                g.arena_generation = best["gen"]
        g.arena_best_score = report.final_score
        # 消融归因放在**择优回滚之后**：归因的对象必须是最终交付的那一代，
        # 否则测的是被丢弃的基因组合，结论对不上产物。
        if ablate:
            self._enter("ablation")
            self._log("正在逐条消融基因，定位拖后腿的那一条")
            report.ablation = self.ablate_genes(g, cases)
            harmful = [v for v in report.ablation.get("variants", [])
                       if v.get("verdict") == "harmful"]
            if harmful:
                self._log(f"  发现 {len(harmful)} 条有害基因"
                          f"（去掉后分数上升），可在下次进化时移除")
        g.save(os.path.join(output_dir, "genome.json"))
        with open(os.path.join(output_dir, "report.json"), "w",
                  encoding="utf-8") as f:
            f.write(report.to_json())
        with open(os.path.join(output_dir, "arena_pairs.json"), "w",
                  encoding="utf-8") as f:
            json.dump(all_pairs, f, ensure_ascii=False, indent=2)
        with open(os.path.join(output_dir, "system_prompt.md"), "w",
                  encoding="utf-8") as f:
            f.write(f"# {g.name} — distilled by OmegaForge\n\n"
                    f"```\n{g.system_prompt}\n```\n")
        # 不可比时不能写"蒸馏体胜出"：verdict 仍是 win，但 结论是否成立 为假，
        # 直接展示结论词会让用户把一个无效的对照当成"更强"的证据。
        if report.claim_valid:
            head = VERDICT_TEXT.get(report.verdict, report.verdict)
        elif report.self_certified:
            head = ("蒸馏完成（裁判与作答为同一模型，当前结论属自我评分，"
                    "本次胜负仅供参考）")
        elif report.contaminated_cases:
            head = (f"蒸馏完成（{report.contaminated_cases} 个用例的评分"
                    f"可能被被测方操纵，本次胜负仅供参考）")
        elif report.debiased_cases < report.arena_cases:
            head = (f"蒸馏完成（{report.arena_cases - report.debiased_cases}"
                    f" 个用例未完成去偏，本次胜负仅供参考）")
        else:
            head = "蒸馏完成（对照组不可比，本次胜负仅供参考）"
        self._log(f"{head}：蒸馏体 {report.final_score:.2f} 分，"
                  f"对照 {report.baseline_score:.2f} 分")
        return g, report
