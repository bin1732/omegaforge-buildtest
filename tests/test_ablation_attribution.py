# -*- coding: utf-8 -*-
"""基因消融归因 —— 回答"哪条基因导致变差"。

为什么需要
----------
进化只知道"分数变了"，不知道"是哪条基因导致的"。于是下一次进化
仍在盲改：批评意见一次覆盖全部基因，有害基因会被反复加回来。

本文件最要紧的一条是**判定方向**：
  delta = 变体分 - 完整分
  delta > 0 → 去掉它反而变好 → 该基因**有害**
  delta < 0 → 去掉它变差   → 该基因**有益**

这是整个功能里最容易写反的地方，且写反后**不报错**——报告照样
生成，只是每条基因的结论全部反过来。所以两个方向都必须有守卫。
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest # noqa: E402

from omegaforge.core.budget import BudgetExceeded, TokenBank # noqa: E402
from omegaforge.core.errors import UserError # noqa: E402
from omegaforge.distill.engine import DistillEngine # noqa: E402
from omegaforge.llm.client import LLMClient # noqa: E402

_CASES = [{"id": "c1", "input": "把这段文本按句切分", "rubric": ["正确性"]}]


def _engine(monkeypatch, *, judge_by_gene_count, sign=+1, budget=9_000_000):
  """构造引擎，并接管评分。

  judge_by_gene_count: 让分数随"基因条数"线性变化，从而可以精确
  控制"去掉一条基因后分数是升还是降"，以此锁死判定方向。

  sign=+1 → 基因越少分越高（去掉一条 → 分上升 → 应判 harmful）
  sign=-1 → 基因越多分越高（去掉一条 → 分下降 → 应判 beneficial）
  """
  eng = DistillEngine(LLMClient(), TokenBank(budget), arena_rounds=1,
            max_generations=1, verbose=False, eval_set=_CASES)

  def fake_answer(g, task):
    # 把**基因内容**（而非条数）编码进答案，供 judge 读取。
    #
    # 教训：第一版只编码条数，于是去掉 u1 / u2 / u3 得到的都是
    # "少一条"，分数完全相同 → discriminative 恒为假，"区分度"
    # 那条守卫自己也在报错。归因的前提就是不同变体产出不同结果，
    # 替身必须真的让它们不同。
    import zlib
    key = ("|".join(sorted(g.upgrade_genes or [])) + "#"
        + "|".join(sorted(g.persona_genes or [])))
    return f"N={zlib.crc32(key.encode('utf-8')) % 7 + 1}"

  def fake_judge(base, ans_a, ans_b):
    def score(ans):
      n = 0
      for tok in str(ans).split():
        if tok.startswith("N="):
          n = int(tok[2:])
      return 5.0 + sign * n

    # a/b 各自按自己的答案打分：N 越大分越高（或越低）
    sa, sb = score(ans_a), score(ans_b)
    return {"a": sa, "b": sb,
        "contamination": {"A": [], "B": []},
        "winner": "A" if sa >= sb else "B", "reason": ""}

  monkeypatch.setattr(eng, "_simulate_answer", fake_answer)
  monkeypatch.setattr(eng, "_simulate_baseline", lambda t: "N=0")
  monkeypatch.setattr(eng, "_judge_once", fake_judge)
  return eng


def _genome():
  """一个有 3 条 upgrade 基因的 genome（走真实 mock synthesize）。"""
  eng = DistillEngine(LLMClient(), TokenBank(9_000_000), arena_rounds=1,
            max_generations=1, verbose=False, eval_set=_CASES)
  from omegaforge.distill.genome import Genome
  g = Genome(name="t", mission_one_liner="m", source_fingerprint="fp")
  g.persona_genes = ["p1", "p2"]
  g.upgrade_genes = ["u1", "u2", "u3"]
  g.workflow_genes = ["w1"]
  return eng, g


# -- 判定方向（本功能最容易写反处）----------------------------------------

def test_removing_gene_that_raises_score_is_harmful(monkeypatch):
  """去掉后分数上升 → 该基因有害。

  sign=-1：分数随基因条数**递减**，于是去掉一条 → 分上升。
  """
  eng, g = _genome()
  eng = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  res = eng.ablate_genes(g, _CASES, max_variants=2)
  vs = [v for v in res["variants"] if "error" not in v]
  assert vs, f"没有产出变体：{res}"
  assert all(v["verdict"] == "harmful" for v in vs), \
    f"去掉后分数上升应判 harmful，实际 {[(v['gene'], v['verdict']) for v in vs]}"
  assert all(v["delta"] > 0 for v in vs)


def test_removing_gene_that_lowers_score_is_beneficial(monkeypatch):
  """去掉后分数下降 → 该基因有益。

  sign=+1：分数随基因条数**递增**，于是去掉一条 → 分下降。

  注意事项：这两个 sign 我第一版**整体写反了**，于是"方向测试"
  自己也在报错——恰恰印证了判定方向极易写反。
  """
  eng, g = _genome()
  eng = _engine(monkeypatch, judge_by_gene_count=True, sign=+1)
  res = eng.ablate_genes(g, _CASES, max_variants=2)
  vs = [v for v in res["variants"] if "error" not in v]
  assert all(v["verdict"] == "beneficial" for v in vs), \
    f"去掉后分数下降应判 beneficial，实际 {[(v['gene'], v['verdict']) for v in vs]}"
  assert all(v["delta"] < 0 for v in vs)


def test_noise_below_threshold_is_neutral(monkeypatch):
  """小于阈值不算信号——把噪声当信号会持续接受没用的基因。"""
  eng, g = _genome()
  # 基因条数变化但分数不变 → delta 恒为 0 → neutral
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=+1)
  monkeypatch.setattr(e2, "_judge_once",
            lambda base, a, b: {"a": 5.0, "b": 5.0,
                      "contamination": {"A": [], "B": []},
                      "winner": "tie", "reason": ""})
  res = e2.ablate_genes(g, _CASES, max_variants=2)
  vs = [v for v in res["variants"] if "error" not in v]
  assert all(v["verdict"] == "neutral" for v in vs)


def test_identical_variant_scores_mark_not_discriminative(monkeypatch):
  """所有变体得分一模一样 → 归因没有分辨力，必须标为不可采信。

  这不是假想：mock 下真实跑出来 6 个变体的 delta 全是 -1.92，
  看着像"每条基因贡献相同且都有益"，实际是去掉任意一条后合成出的
  提示词退化成了同一个东西。不标记的话，用户会照着一份分辨力
  为零的报告去删基因。
  """
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=+1)
  monkeypatch.setattr(e2, "_judge_once", lambda base, a, b: {
    "a": 5.0, "b": 5.0, "contamination": {"A": [], "B": []},
    "winner": "tie", "reason": ""})
  res = e2.ablate_genes(g, _CASES, max_variants=3)
  assert res["discriminative"] is False
  assert res["comparable"] is False
  assert "未产生区分" in res.get("note", "")


def test_distinct_variant_scores_are_discriminative(monkeypatch):
  """反方向守卫：分数确实不同时不能误标为无分辨力。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=+1)
  res = e2.ablate_genes(g, _CASES, max_variants=3)
  assert res["discriminative"] is True


def test_ablation_persists_into_report(monkeypatch):
  """消融结论必须落进 report.json，否则用户拿不到产物里的归因。

  走真实 distill（mock LLM），不是只调方法——只调方法验不出
  "有没有接进主流程"，这坑本项目已踩过多次。
  """
  import json
  import tempfile
  eng, g = _genome()
  with tempfile.TemporaryDirectory() as d:
    src = os.path.join(d, "src.py")
    pathlib.Path(src).write_text(
      "You are a helpful assistant that writes clean Python code. "
      "Always explain your reasoning step by step and return only "
      "the requested output.", encoding="utf-8")
    e2 = DistillEngine(LLMClient(), TokenBank(9_000_000), arena_rounds=1,
              max_generations=1, verbose=False, eval_set=_CASES)
    _g, r = e2.distill(src, output_dir=os.path.join(d, "o"), ablate=True)
    disk = json.loads(pathlib.Path(
      os.path.join(d, "o", "report.json")).read_text(encoding="utf-8"))
    assert "ablation" in disk, "产物 report.json 缺少 ablation"
    assert disk["ablation"].get("variants"), "产物里的消融没有变体结论"
    # 未开启时不应写
  with tempfile.TemporaryDirectory() as d:
    src = os.path.join(d, "src.py")
    pathlib.Path(src).write_text(
      "You are a helpful assistant that writes clean Python code.",
      encoding="utf-8")
    e3 = DistillEngine(LLMClient(), TokenBank(9_000_000), arena_rounds=1,
              max_generations=1, verbose=False, eval_set=_CASES)
    e3.distill(src, output_dir=os.path.join(d, "o"))
    disk = json.loads(pathlib.Path(
      os.path.join(d, "o", "report.json")).read_text(encoding="utf-8"))
    assert disk.get("ablation") == {}, "默认不该跑消融（成本不低）"


# -- 预算与降级：附加诊断不该拖垮整轮蒸馏 ----------------------------------

def test_budget_exhaustion_aborts_without_raising(monkeypatch):
  """消融只是诊断，烧光预算也必须让主流程继续。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)

  def boom(*a, **k):
    raise BudgetExceeded("out")

  monkeypatch.setattr(e2, "_simulate_answer", boom)
  res = e2.ablate_genes(g, _CASES, max_variants=3)
  assert res.get("aborted") is True
  assert "预算" in res.get("note", "")


def test_variant_phase_budget_exhaustion_aborts(monkeypatch):
  """变体**阶段**烧光预算也必须降级，而且必须是在变体循环里兜住的。

  为什么要单独测这一处：回退校验时我撤掉了变体循环里的
  BudgetExceeded 捕获，测试竟然**全绿**——因为基线评分那一步
  也有同样的 except，把它兜住了。两处兜底互相兜底，于是单撤
  一处永远抓不到。**回退必须撤到真正的生效点**，这次又是同一教训。
  """
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=+1)
  real_answer = e2._simulate_answer
  calls = {"n": 0}

  def flaky(g_, task):
    calls["n"] += 1
    if calls["n"] == 1:      # 基线评分放行
      return real_answer(g_, task)
    raise BudgetExceeded("out")  # 进入变体阶段后耗尽

  monkeypatch.setattr(e2, "_simulate_answer", flaky)
  res = e2.ablate_genes(g, _CASES, max_variants=3)
  assert res.get("aborted") is True
  assert "变体" in res.get("note", ""), \
    f"应标明是在第几个变体处耗尽，实际 {res.get('note')}"


def test_single_variant_failure_does_not_kill_the_rest(monkeypatch):
  """某个变体合成失败，其余仍要给出结论。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  real = e2.step_synthesize
  calls = {"n": 0}

  def flaky(v):
    calls["n"] += 1
    if calls["n"] == 1:
      raise UserError("合成失败")
    return real(v)

  monkeypatch.setattr(e2, "step_synthesize", flaky)
  res = e2.ablate_genes(g, _CASES, max_variants=3)
  errs = [v for v in res["variants"] if "error" in v]
  oks = [v for v in res["variants"] if "error" not in v]
  assert errs and oks, f"应既有失败也有成功：{res['variants']}"


def test_no_cases_means_no_ablation(monkeypatch):
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  res = e2.ablate_genes(g, [], max_variants=3)
  assert res["variants"] == [] and "无评测用例" in res["note"]


# -- 可信度标记 ------------------------------------------------------------

def test_contaminated_variant_marks_incomparable(monkeypatch):
  """变体答案有污染痕迹时，归因结论不可采信，必须标出来。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  monkeypatch.setattr(e2, "_judge_once", lambda base, a, b: {
    "a": 5.0, "b": 5.0,
    # 蒸馏体侧带污染：顺序1 在 B 位
    "contamination": {"A": [], "B": ["self_praise"]},
    "winner": "tie", "reason": ""})
  res = e2.ablate_genes(g, _CASES, max_variants=2)
  assert res["comparable"] is False, "有污染的归因不可采信，必须标记"


def test_truncated_flag_when_genes_exceed_cap(monkeypatch):
  """超过上限只测前几条，必须如实标记"没测全"。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  res = e2.ablate_genes(g, _CASES, max_variants=1)
  assert res["truncated"] is True


def test_upgrade_genes_are_ablated_first(monkeypatch):
  """upgrade 是进化产物，最可能有害，应优先消融。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  res = e2.ablate_genes(g, _CASES, max_variants=2)
  assert all(v["kind"] == "upgrade_genes" for v in res["variants"])


def test_variant_really_removes_the_gene(monkeypatch):
  """变体必须真的少一条基因——否则消融测的是同一个东西。"""
  eng, g = _genome()
  e2 = _engine(monkeypatch, judge_by_gene_count=True, sign=-1)
  seen = []

  def spy(v):
    seen.append(len(v.upgrade_genes or []))
    return e2._orig_synth(v)

  real_synth = e2.step_synthesize
  e2._orig_synth = real_synth
  monkeypatch.setattr(e2, "step_synthesize", spy)
  e2.ablate_genes(g, _CASES, max_variants=3)
  assert seen == [2, 2, 2], f"每个变体都应少一条，实际 {seen}"


if __name__ == "__main__":
  sys.exit(pytest.main([__file__, "-q"]))
