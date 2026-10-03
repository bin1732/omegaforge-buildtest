"""DistillEngine 契约守卫（pytest，CI 会真实执行）。

为什么需要这一份
----------------
engine.py 是蒸馏主体（540+ 行），缺少该约束时**未被专项审计过**。
本文件覆盖的 6 个缺陷全部由"注入受控 LLM 响应 + 真实跑通 distill()"
可复现，不是读代码推出来的。它们有三个共同特征：

 1. 都不崩溃（除 2 个），所以 183 项既有测试一次都没碰到
 2. 都**扭曲结论本身**，而不是报错——用户拿到的是看起来正常的错误答案
 3. 都发生在"模型返回了 JSON 但内容不规整"的路径上，
   而 mock 环境里模型永远返回规整数据，所以永远测不到

最严重的是第 1 条（judge 分数被系统性压低 40%）：
它直接扭曲"蒸馏体是否更强"这个产品唯一的核心结论。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omegaforge.core.budget import TokenBank          # noqa: E402
from omegaforge.distill.engine import (             # noqa: E402
  DistillEngine, _as_gene_list, _as_token_count, _avg_scores)

RICH_SOURCE = (
  "You are ArxivResearcher, a meticulous academic research assistant. "
  "Your mission: locate and summarize academic papers with rigor. "
  "Always cite sources with arXiv IDs. Never fabricate DOIs. "
  "Tools: arxiv_search(query). Workflow: parse -> search -> synthesize."
)


class _Resp:
  def __init__(self, text, model="fake"):
    self.text = text
    self.model = model
    self.prompt_tokens = 10
    self.completion_tokens = 10


class FakeLLM:
  """按阶段返回受控内容，用于逼出"模型返回不规整 JSON"的真实行为。"""

  model_fast = model_main = model_judge = "fake"

  def __init__(self, judge=None, comp=None, crit=None):
    self.judge = judge
    self.comp = comp
    self.crit = crit
    self.seen = []
    self._canon = None   # (baseline 文本, distilled 文本)，首次判定确立

  def _judge_payload(self, user: str):
    """按"答案本身"给分，而不是按"答案所在位置"给分。

    双顺序上线后，同一个用例会被评两次、两次的 A/B 位置对调。
    若替身无视位置、每次都返回同一份 请求体，它就等价于一个
    **100% 偏袒 A 位**的裁判——去偏后两侧必然扯平，反而掩盖了
    被测试的性质。所以替身必须认答案：哪段文本是首次见到的 A，
    它就该一直拿 A 的分数，无论后来被放到哪个位置。
    """
    payload = self.judge if self.judge is not None else self._default_judge()
    try:
      tail = user.split("ANSWER A:", 1)[1]
      a_txt, b_txt = tail.split("ANSWER B:", 1)
    except (IndexError, ValueError):
      return payload
    a_txt, b_txt = a_txt.strip(), b_txt.strip()
    if self._canon is None:
      self._canon = (a_txt, b_txt)
    base_txt, dist_txt = self._canon
    if a_txt == base_txt and b_txt == dist_txt:
      return payload
    if a_txt == dist_txt and b_txt == base_txt:
      sc = payload.get("scores", {})
      swapped = dict(payload)
      swapped["scores"] = {"A": sc.get("B", {}), "B": sc.get("A", {})}
      w = payload.get("winner", "tie")
      swapped["winner"] = {"A": "B", "B": "A"}.get(w, "tie")
      return swapped
    return payload

  def chat(self, system, user, model=None, max_tokens=2048,
       json_mode=False, **kw):
    # 注意顺序：CRITIQUE_提示词 里也含 "ANSWER A:"，
    # 必须先判 critique 再判 judge，否则会取错 请求体。
    if "targeted gene mutations" in user:
      self.seen.append("critique")
      return _Resp(json.dumps(self.crit or {"deltas": ["d1"]}))
    if "ANSWER A:" in user:
      self.seen.append("judge")
      return _Resp(json.dumps(self._judge_payload(user)))
    if "Compress this agent SPEC" in user:
      return _Resp(json.dumps(self.comp if self.comp is not None
                  else self._default_comp()))
    if "Reconstruct a precise SPEC" in user:
      return _Resp(json.dumps({
        "name": "SrcAgent", "role": "r", "mission": "m",
        "persona": "p", "tools": [], "workflow": [], "io": {},
        "failure_modes": [], "quality_bars": []}))
    if "Compile this Genome" in user:
      return _Resp(json.dumps({
        "system_prompt": "You are X. " * 20, "tools": [],
        "workflow": [], "design_notes": "n"}))
    if "Design a compact exam" in user:
      return _Resp(json.dumps({"cases": [
        {"id": "c1", "input": "t1", "rubric": ["r1"]}]}))
    # 作答调用：system 是 agent 提示词（蒸馏体/对照组各一份），
    # 而 JSON 类调用的 system 统一是 "You are OmegaForge ..."。
    # 必须让两侧答案**内容不同**，否则双顺序下替身无法判断哪段是
    # 对照组、哪段是蒸馏体（两份答案一模一样时，"按答案给分"无意义）。
    if "You are OmegaForge" not in system:
      return _Resp("DISTILLED_ANSWER"
             if "You are X." in system else "BASELINE_ANSWER")
    return _Resp("{}")

  @staticmethod
  def _default_judge():
    return {"scores": {"A": {"task_completion": 5},
              "B": {"task_completion": 5}},
        "reason": "r", "winner": "tie"}

  @staticmethod
  def _default_comp():
    return {"name": "N", "mission_one_liner": "m",
        "persona_genes": ["a"], "tool_genes": ["t"],
        "workflow_genes": ["w"], "upgrade_genes": ["u"],
        "est_system_tokens": 100}


def _run(llm, **kw) -> tuple:
  eng = DistillEngine(llm, TokenBank(500_000), arena_rounds=1,
            max_generations=1, verbose=False, **kw)
  tmp = tempfile.mkdtemp(prefix="of_engine_")
  return eng.distill(RICH_SOURCE, output_dir=os.path.join(tmp, "out"))


# ---------------------------------------------------------------- 1. 评分口径
def test_partial_numeric_scores_not_diluted():
  """judge 只回了 3 个数字维度时，不能除以 5 个维度数。

  验证：{"task_completion": 8, "correctness": "good", "efficiency": 7,
      "robustness": null, "polish": 9}
  旧实现 sum/max(1,len(sa)) = 24/5 = 4.80，真实水平 24/3 = 8.00。
  分数被压低 40%，而它正是"更强"结论的唯一依据。
  """
  judge = {"scores": {
    "A": {"task_completion": 8, "correctness": "good",
       "efficiency": 7, "robustness": None, "polish": 9},
    "B": {"task_completion": 3, "correctness": 2,
       "efficiency": 4, "robustness": 1, "polish": 2}},
    "reason": "r", "winner": "A"}
  _, rep = _run(FakeLLM(judge=judge))
  assert abs(rep.baseline_score - 8.0) < 0.01, (
    f"对照分应为 (8+7+9)/3=8.00， {rep.baseline_score:.2f}")


def test_avg_scores_rejects_non_dict_and_bool():
  """scores.A 不是 dict 时不崩溃；bool 不算作分数（True 会被当 1 分）。"""
  assert _avg_scores("not a dict") == 0.0
  assert _avg_scores(None) == 0.0
  assert _avg_scores(42) == 0.0
  assert _avg_scores([]) == 0.0
  assert _avg_scores({}) == 0.0
  # True 是 int 子类，但不能当 1 分计入，否则 4 个维度变 5 个
  assert _avg_scores({"a": 10, "b": True}) == 10.0


def test_non_dict_scores_does_not_crash_distill():
  """模型把 A 写成字符串/数字/数组/空 → 整轮蒸馏不能崩。

  旧实现 .values() 直接 AttributeError，冒泡成"操作失败，请稍后重试"，
  把上游数据问题伪装成服务器故障。
  """
  for bad in ["not a dict", 42, ["x"], None]:
    _, rep = _run(FakeLLM(judge={"scores": {"A": bad, "B": {"t": 5}}}))
    # 只断言"不崩溃"是不够的：把两个维度一起清零同样不崩溃，
    # 却会把合法的那一维也抹掉，结论照样被扭曲。
    assert abs(rep.final_score - 5.0) < 0.01, (
      f"脏维度只应影响自己，蒸馏体得分仍应为 5.00，"
      f"实际 {rep.final_score:.2f}")
    assert abs(rep.baseline_score - 0.0) < 0.01, (
      f"脏维度自身应记 0 分，实际 {rep.baseline_score:.2f}")


def test_scores_container_non_dict_does_not_crash():
  """scores 字段本身不是对象时也不能崩。

  本条是被**回退校验**逼出来的盲区：上面那条只覆盖了 "scores 是字典、
  但 A 不是"，没覆盖 "scores 整体不是字典"。
  回退修复时它照样全绿（形式化），说明漏了一层。
  旧实现 data.get("scores", {}).get("A", {}) 在 scores 为字符串时
  会对 str 调 .get() → AttributeError。
  """
  for bad in ["not a dict", 42, ["x"], None, True]:
    _, rep = _run(FakeLLM(judge={"scores": bad, "reason": "r"}))
    # 评分整体不可用时必须记 0，而不是留下上一次的残留或随手填的数。
    # 只验"不崩溃"的话，填进任何值都测不出来。
    assert rep.baseline_score == 0.0 and rep.final_score == 0.0, (
      f"评分整体不可用时应记 0 分，实际 对照={rep.baseline_score} "
      f"蒸馏体={rep.final_score}")


# ------------------------------------------------------------ 2. 理由不丢失
def test_judge_reasons_present_on_win():
  """胜出时也必须带评审理由。

  验证：旧实现只在 loss 分支赋 judge_reasons，于是 verdict=win
  时报告里的理由列表是空的——用户拿到"更强"的结论，
  却看不到任何一条支撑它的评审意见，结论不可复核。
  恰恰是赢了才最需要证据。
  """
  judge = {"scores": {"A": {f"d{i}": 1 for i in range(5)},
            "B": {f"d{i}": 9 for i in range(5)}},
       "reason": "蒸馏体明显更好", "winner": "B"}
  _, rep = _run(FakeLLM(judge=judge))
  assert rep.verdict == "win", f"应为 win， {rep.verdict}"
  assert rep.judge_reasons == ["蒸馏体明显更好"], (
    f"胜出时理由丢失: {rep.judge_reasons}")


def test_judge_reasons_present_on_tie():
  judge = {"scores": {"A": {f"d{i}": 5 for i in range(5)},
            "B": {f"d{i}": 5 for i in range(5)}},
       "reason": "双方持平", "winner": "tie"}
  _, rep = _run(FakeLLM(judge=judge))
  assert rep.verdict == "tie"
  assert rep.judge_reasons == ["双方持平"]


# -------------------------------------------------------------- 3. 基因净化
def test_string_deltas_not_split_into_chars():
  """deltas 是字符串时不能被逐字符拆开。

  验证：{"deltas": "abcdef"} 走 [].extend(str) 会变成
  ['a','b','c','d','e','f']——基因库被垃圾单字符污染，
  后续 synthesize 拿到无意义基因，且不报任何错。
  """
  assert _as_gene_list("abcdef") == ["abcdef"]
  assert _as_gene_list({"a": 1, "b": 2}) == ["1", "2"]  # 取值不是取键
  assert _as_gene_list(None) == []
  assert _as_gene_list(42) == []
  assert _as_gene_list(["d1", "d2", {"x": 1}, None]) == ["d1", "d2"]

  judge = {"scores": {"A": {f"d{i}": 9 for i in range(5)},
            "B": {f"d{i}": 1 for i in range(5)}}}
  g, rep = _run(FakeLLM(judge=judge, crit={"deltas": "abcdef"}))
  assert "abcdef" in g.upgrade_genes, f"基因被污染: {g.upgrade_genes}"
  # 基线 upgrade_genes 是 ["u"]，正确追加 1 条后共 2 条；
  # 若被逐字符拆开则是 1+6=7 条。用条数判定，避免误伤合法的单字基因。
  assert len(g.upgrade_genes) == 2, (
    f"字符串被逐字符拆开污染: {g.upgrade_genes}")


# ------------------------------------------------------------ 4. 数值收敛
def test_token_count_coerced():
  """est_system_tokens 为字符串/空/负数时不能崩，也不能产生负压。

  验证：字符串 "100" 进 max(1, x) 抛 TypeError:
  '>' not supported between instances of 'str' and 'int'，
  整轮蒸馏在压缩阶段就崩。
  """
  assert _as_token_count("100") == 100
  assert _as_token_count(None) == 0
  assert _as_token_count(-5) == 0
  assert _as_token_count("abc") == 0
  assert _as_token_count(0) == 0

  for bad in ["100", None, -5, "abc"]:
    comp = dict(FakeLLM._default_comp(), est_system_tokens=bad)
    _run(FakeLLM(comp=comp))


# -------------------------------------------------------- 5. name 不出现 None
def test_null_name_does_not_crash_or_leak_none():
  """模型返回 name=null 时，产物不能崩，也不能写出 'None'。

  验证：旧实现 data.get("name", spec.name) 拿到 None
  （键存在、值为空，默认值不生效），随后
  .replace("<<ROLE>>", g.name) 抛 TypeError:
  replace() argument 2 must be str, not None —— 整轮蒸馏崩溃。
  """
  comp = dict(FakeLLM._default_comp(), name=None)
  llm = FakeLLM(comp=comp)
  eng = DistillEngine(llm, TokenBank(500_000), arena_rounds=1,
            max_generations=1, verbose=False)
  tmp = tempfile.mkdtemp(prefix="of_name_")
  out = os.path.join(tmp, "out")
  g, rep = eng.distill(RICH_SOURCE, output_dir=out)

  assert isinstance(g.name, str) and g.name, f"name 非法: {g.name!r}"
  md = open(os.path.join(out, "system_prompt.md"), encoding="utf-8").read()
  assert "None" not in md, f"产物泄漏 None: {md.splitlines()[0]!r}"
  gj = open(os.path.join(out, "genome.json"), encoding="utf-8").read()
  assert '"name": null' not in gj, "genome.json 写出 null name"


def test_system_prompt_non_string_is_coerced():
  """synthesize 返回 system_prompt 为对象/数字时收敛为字符串。"""
  class _L(FakeLLM):
    def chat(self, system, user, model=None, max_tokens=2048,
         json_mode=False, **kw):
      if "Compile this Genome" in user:
        return _Resp(json.dumps({"system_prompt": {"bad": 1},
                     "tools": [], "workflow": [],
                     "design_notes": "n"}))
      return super().chat(system, user, model=model,
                max_tokens=max_tokens, json_mode=json_mode, **kw)

  g, _ = _run(_L())
  assert isinstance(g.system_prompt, str), (
    f"system_prompt 应为 str， {type(g.system_prompt)}")
