# -*- coding: utf-8 -*-
"""自进化端到端守卫：跑真实主循环，验证交付的是**最佳一代**。

为什么必须端到端
----------------
本文件的前身用 AST 断言"主循环里存在 _select_best_generation / _restore
调用"。回退校验时**两项没抓到**：把 `if not improved:` 改成 `if False:`、
把末尾收口改成 `if False:`，AST 断言照样全绿——因为调用语句**语法上还在**，
只是永远进不去。这正是本项目反复踩的"只测存在、不测行为"。

所以这里改为构造引擎、跑真实 distill 主循环、检查最终产物。
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import omegaforge.distill.engine as E # noqa: E402
from omegaforge.core.budget import TokenBank # noqa: E402
from omegaforge.distill.engine import DistillEngine # noqa: E402
from omegaforge.distill.genome import Genome # noqa: E402
from omegaforge.domain.baseline import Baseline # noqa: E402

_REAL_LOADER = E.SourceAgentLoader


def _engine(scores, baseline_score=9.0, max_generations=3):
  """构造一台跑假竞技场的引擎。scores[i] = 第 i+1 代蒸馏体得分。"""
  eng = DistillEngine.__new__(DistillEngine)
  eng.bank = TokenBank(9_000_000)
  eng.arena_rounds = 2
  eng.max_generations = max_generations
  eng.verbose = False
  eng.task = ""
  eng.llm = None
  eng.on_phase = None
  eng.baseline = Baseline(kind="provided", system_prompt="p",
              note="", source_ref="r")
  eng.arena_cases = 0
  eng.arena_contaminated = 0
  eng.arena_debiased = 0
  eng.question_source = "builtin"
  eng.question_model = ""
  eng.rubric_source = "builtin"
  eng.eval_set = None     # 本套件只验代际回滚，不沿用评测集
  eng._enter = lambda p: None
  eng._log = lambda m: None

  def fake_arena(g, cases, gen):
    d = scores[gen - 1]
    pair = {"case": "c1", "task": "t1",
        "answer_baseline": "b", "answer_distilled": "d",
        "score_baseline": baseline_score, "score_distilled": d,
        "judge_reason": "差距", "winner": "baseline",
        "debiased": True, "position_bias": 0.0,
        "orders_agree": True,
        "contamination": {"distilled": [], "baseline": []},
        "contaminated": False}
    eng.arena_cases = 1
    eng.arena_debiased = 1
    eng.arena_contaminated = 0
    return d, baseline_score, ["理由"], [pair]

  def fake_evolve(g, task, reason, a, b):
    g.upgrade_genes.append(f"gene-gen{g.arena_generation}")
    return g

  eng.run_arena = fake_arena
  eng.evolve_step = fake_evolve
  eng.step_extract = lambda sig: None
  eng.step_compress = lambda spec, lineage, fingerprint: Genome(
    name="x", mission_one_liner="m",
    source_fingerprint="f", lineage=lineage)
  eng.step_synthesize = lambda g: g
  eng.step_gen_eval = lambda g: [{"task": "t1"}]

  class _Sig:
    fingerprint = "fp"

    def summary(self):
      return "s"

  class _Loader:
    def load(self, s):
      return _Sig()

  E.SourceAgentLoader = _Loader
  try:
    yield eng
  finally:
    E.SourceAgentLoader = _REAL_LOADER


def _run(scores, **kw):
  for eng in _engine(scores, **kw):
    with tempfile.TemporaryDirectory() as d:
      return eng.distill("src", output_dir=d)


def test_regression_rolls_back_to_best():
  """越进化越差 → 交付最佳那代，而不是最后一代。

  缺少该约束时：final_score=最后一代 3.0、交付的基因带满 3 次进化痕迹，
  而第 1 代的 8.0 才是最好成绩——用户拿到的是历代最差的基因组。
  """
  g, r = _run([8.0, 5.0, 3.0])
  assert r.final_score == 8.0, f"应交付最佳 8.0，实际 {r.final_score}"
  assert r.best_generation == 1, f"最佳应为第 1 代，实际 {r.best_generation}"
  assert r.generations_run == 3, "三代都应实跑"
  assert r.rolled_back is True, "发生过回退但未记录"
  assert g.upgrade_genes == [], \
    f"应回滚到第 1 代（无进化基因），实际 {g.upgrade_genes}"
  assert g.arena_best_score == 8.0, "best 字段应记最佳值，不是最后一代"
  assert any("回滚" in n for n in r.evolution_notes), \
    "回滚未写进 evolution_notes，用户无从得知"


def test_sustained_improvement_keeps_genes():
  """持续改善 → 基因累积保留，不被误回滚。

  这条是防"处理过头"：只回滚不晋升的话，真进步也会被丢掉，
  进化就退化成"原地不动"。
  """
  g, r = _run([4.0, 6.0, 8.0])
  assert r.final_score == 8.0, f"应交付 8.0，实际 {r.final_score}"
  assert r.best_generation == 3, f"最佳应为第 3 代，实际 {r.best_generation}"
  assert r.rolled_back is False, "持续改善不应回滚"
  assert len(g.upgrade_genes) == 3, \
    f"三代进化都应保留，实际 {g.upgrade_genes}"


def test_win_breaks_and_delivers_that_generation():
  """首代即胜 → 立即结束，交付首代。"""
  g, r = _run([9.5], baseline_score=2.0)
  assert r.verdict == "win"
  assert r.best_generation == 1
  assert r.final_score == 9.5


def test_marginal_gain_not_promoted_e2e():
  """噪声级改善不晋升：0.03 的波动不能算进步。"""
  # 第 2 代 5.03 vs 第 1 代 5.0 → 未改善 → 回滚到第 1 代
  g, r = _run([5.0, 5.03], max_generations=2)
  assert r.best_generation == 1, \
    f"0.03 波动不应晋升，实际最佳第 {r.best_generation} 代"
  assert g.upgrade_genes == [], "未改善不应保留新基因"


if __name__ == "__main__":
  import pytest
  sys.exit(pytest.main([__file__, "-q"]))
