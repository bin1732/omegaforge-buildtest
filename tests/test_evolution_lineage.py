# -*- coding: utf-8 -*-
"""自进化骨架守卫：可版本化、可评测、可回滚。

进化的三条腿缺一条就不是进化：
 · 可版本化 → 每代留快照（_snapshot 存 JSON，防就地改写）
 · 可评测  → 每代在**同一份**冻结用例上打分
 · 可回滚  → 没改善就退回最佳一代

缺少该约束时最严重的失效：进化就地改写 upgrade_genes 且**从不回滚**，
于是越改越差的几代已经写进 genome，而产物保存的是最后一代——
用户拿到的可能恰好是历代最差的基因组，报告却写着"已进化 N 代"。
同时 `arena_best_score` 赋的是 final（最后一代）而不是历届最大值，
字段名与语义不符，进一步掩盖了这件事。

本文件既测方法，也测**主循环接通**（第 7 项）。只测方法不够——
接通断了方法照样全绿，这是本项目反复踩过的坑。
"""
import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from omegaforge.distill.engine import DistillEngine # noqa: E402
from omegaforge.distill.genome import Genome # noqa: E402

SRC = (pathlib.Path(__file__).resolve().parents[1]
    / "omegaforge/distill/engine.py")


def _genome(genes, prompt="p"):
  g = Genome(name="x", mission_one_liner="m", source_fingerprint="f")
  g.upgrade_genes = list(genes)
  g.system_prompt = prompt + "".join(genes)
  return g


def test_no_improvement_means_rollback():
  """进化后分数变差 → 不晋升，且能回滚出上一代的基因。"""
  g1 = _genome(["g1"])
  snap = DistillEngine._snapshot(g1, 1, 5.0)
  g2 = _genome(["g1", "g2"])         # 进化后的基因组
  best, improved = DistillEngine._select_best_generation(
    snap, 2, 3.0, g2.to_json())
  assert improved is False, "分数下降不应判定为改善"
  assert best["gen"] == 1
  restored = DistillEngine._restore(best)
  assert restored.upgrade_genes == ["g1"], \
    f"回滚未恢复上一代基因：{restored.upgrade_genes}"


def test_real_improvement_promotes():
  """明确改善 → 晋升，交付新基因。"""
  snap = DistillEngine._snapshot(_genome(["g1"]), 1, 5.0)
  g2 = _genome(["g1", "g2"])
  best, improved = DistillEngine._select_best_generation(
    snap, 2, 7.0, g2.to_json())
  assert improved is True
  assert best["gen"] == 2
  assert DistillEngine._restore(best).upgrade_genes == ["g1", "g2"]


def test_marginal_gain_is_not_improvement():
  """噪声级改善不算改善。

  把波动当进步是自进化最危险的失效：它会持续接受其实没用的基因，
  而每一代都要花 token 和一轮评测。
  """
  snap = DistillEngine._snapshot(_genome(["g1"]), 1, 5.0)
  best, improved = DistillEngine._select_best_generation(
    snap, 2, 5.03, _genome(["g1", "g2"]).to_json())
  assert improved is False, "0.03 的波动应视为无改善"


def test_snapshot_survives_inplace_mutation():
  """快照必须防就地改写——存引用等于没存。"""
  g = _genome(["a"])
  snap = DistillEngine._snapshot(g, 1, 1.0)
  g.upgrade_genes.append("b")
  assert DistillEngine._restore(snap).upgrade_genes == ["a"]


def test_first_generation_always_promotes():
  g = _genome(["a"])
  best, improved = DistillEngine._select_best_generation(
    None, 1, 4.0, g.to_json())
  assert improved is True and best["gen"] == 1


def test_empty_snapshot_raises_instead_of_silence():
  """快照残缺必须报错，绝不静默返回一个空 genome。"""
  from omegaforge.core.errors import UserError
  try:
    DistillEngine._restore({})
  except UserError:
    return
  except Exception as exc: # noqa: BLE001
    raise AssertionError(f"应抛 UserError，实际 {type(exc).__name__}")
  raise AssertionError("残缺快照被静默通过了")


def test_main_loop_is_wired():
  """接线：主循环必须真的调用择优与回滚。

  ⚠️ 能力边界（验证）：此项**只能防"整段被删"**，防不住"分支被改坏"。
  回退校验时把 `if not improved:` 改成 `if False:`、把末尾收口改成
  `if False:`，本项**照样全绿**——调用语句语法上还在，只是永远进不去。
  真正的验证在 test_evolution_lineage_e2e.py（跑真实主循环看产物）。
  本项保留的价值：整段代码被误删时能给一个便宜的早期信号。
  """
  tree = ast.parse(SRC.read_text(encoding="utf-8"))
  calls = {n.func.attr for n in ast.walk(tree)
       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
  assert "_select_best_generation" in calls, "主循环未调用择优晋升"
  assert "_restore" in calls, "主循环未调用回滚"
  # 回滚必须发生在循环**内**（每代末），而不是只在结束时收口一次：
  # 只在最后收口的话，中间几代污染的基因已经被写进 arena_history
  # 并参与后续进化，越改越差照样发生。
  src = SRC.read_text(encoding="utf-8")
  loop = src[src.find("for gen in range(1, self.max_generations"):]
  loop = loop[:loop.find("self._enter(\"finalize\")")]
  assert "_select_best_generation" in loop, "择优不在主循环内"
  assert "self._restore(best)" in loop, "回滚不在主循环内（只在末尾收口不够）"


def test_report_fields_exist():
  """产物必须能回答"交付的是第几代、跑了几代、有没有回滚"。"""
  src = SRC.read_text(encoding="utf-8")
  for f in ("best_generation", "generations_run", "rolled_back"):
    assert f"{f}: int" in src or f"{f}: bool" in src, \
      f"DistillReport 缺少字段 {f}"


if __name__ == "__main__":
  import pytest
  sys.exit(pytest.main([__file__, "-q"]))
