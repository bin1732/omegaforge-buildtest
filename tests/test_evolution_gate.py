# -*- coding: utf-8 -*-
"""进化闸门守卫：进化的方向只能来自可信证据。

背景（验证）：缺少该约束时 evolve_step 取 pairs[0].judge_reason 作为唯一依据，
且完全不看 debiased / contaminated。于是：
 · 去偏整体失败（0/N 完成）时分数仍带位置偏差，却照样驱动进化；
 · 答案含评分操纵痕迹的对，其理由照样被写进基因；
 · 只取第一条，其余所有对的证据被丢弃。

后果不是"进化慢一点"：不可信的判断被编译成基因，并在后续代际继续放大
（arena 的污染只影响一次得分，evolve 的污染会遗传）。

本文件既测方法行为，也测**接通**（第 7、8 项）。只测方法是不够的——
接通断了方法照样全绿，这是本项目反复踩过的坑。
"""
import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from omegaforge.distill.engine import DistillEngine # noqa: E402

SRC = pathlib.Path(__file__).resolve().parents[1] / "omegaforge/distill/engine.py"


def _engine():
  """不触发 __init__ 拿到实例：本方法只读入参，不依赖引擎状态。"""
  return DistillEngine.__new__(DistillEngine)


def _pair(cid="c1", debiased=True, contaminated=False,
     sd=1.0, sb=9.0, reason="差距在X"):
  return {"case": cid, "task": "t-" + cid,
      "answer_baseline": "b", "answer_distilled": "d",
      "score_baseline": sb, "score_distilled": sd,
      "judge_reason": reason, "winner": "baseline",
      "debiased": debiased, "position_bias": 0.0,
      "orders_agree": True,
      "contamination": {"distilled": [], "baseline": []},
      "contaminated": contaminated}


def test_all_undebiased_yields_no_evidence():
  """全部未去偏 → 无可信证据，不应驱动进化。"""
  pairs = [_pair("c1", debiased=False), _pair("c2", debiased=False)]
  assert DistillEngine._select_evolution_evidence(_engine(), pairs) is None


def test_all_contaminated_yields_no_evidence():
  """全部含操纵痕迹 → 无可信证据。污染会遗传，必须挡在进化之外。"""
  pairs = [_pair("c1", contaminated=True), _pair("c2", contaminated=True)]
  assert DistillEngine._select_evolution_evidence(_engine(), pairs) is None


def test_empty_pairs_yields_no_evidence():
  assert DistillEngine._select_evolution_evidence(_engine(), []) is None


def test_trusted_subset_is_selected():
  """混合场景：只采纳去偏完成且未污染的对。"""
  pairs = [_pair("c1", debiased=False),
       _pair("c2", contaminated=True),
       _pair("c3", debiased=True, contaminated=False)]
  ev = DistillEngine._select_evolution_evidence(_engine(), pairs)
  assert ev is not None
  _focus, _gap, n = ev
  assert n == 1, f"应只采纳 1 条可信证据，实际 {n}"


def test_focus_is_worst_losing_pair():
  """聚焦对象应是蒸馏体输得最多的那条，而非列表第一条。"""
  # c1 输得最多（-8），但排在最前；c2 只输 -1
  pairs = [_pair("c1", sd=1.0, sb=9.0), _pair("c2", sd=8.0, sb=9.0)]
  focus, _gap, _n = DistillEngine._select_evolution_evidence(_engine(), pairs)
  assert focus["case"] == "c1"

  # 反转顺序后仍应选中输得最多的那条（不依赖摆放位置）
  rev = [_pair("c2", sd=8.0, sb=9.0), _pair("c1", sd=1.0, sb=9.0)]
  focus2, _g2, _n2 = DistillEngine._select_evolution_evidence(_engine(), rev)
  assert focus2["case"] == "c1", "聚焦不应受摆放顺序影响"


def test_gap_aggregates_multiple_reasons():
  """理由应汇总（去重），而不是只取单条。"""
  pairs = [_pair("c1", sd=1.0, sb=9.0, reason="缺证据"),
       _pair("c2", sd=2.0, sb=9.0, reason="结构乱"),
       _pair("c3", sd=3.0, sb=9.0, reason="缺证据")]
  _focus, gap, _n = DistillEngine._select_evolution_evidence(_engine(), pairs)
  assert "缺证据" in gap and "结构乱" in gap
  assert gap.count("缺证据") == 1, "重复理由应去重"


def test_evolve_call_is_gated_by_evidence():
  """接线守卫：evolve_step 必须位于『无可信证据则不进化』的分支内。

  只测方法不够——若有人把调用改回 `pairs[0]` 直取，方法测试照样全绿，
  而不可信证据又会重新进入基因。
  """
  src = SRC.read_text(encoding="utf-8")
  tree = ast.parse(src)

  calls = [n for n in ast.walk(tree)
       if isinstance(n, ast.Call)
       and getattr(n.func, "attr", None) == "evolve_step"]
  assert calls, "未找到 evolve_step 调用，接线可能已被移除"

  def in_orelse(target):
    for node in ast.walk(tree):
      if isinstance(node, ast.If):
        for br in node.orelse:
          for sub in ast.walk(br):
            if sub is target:
              return True
    return False

  for c in calls:
    assert in_orelse(c), ("evolve_step 调用不在 else 分支内，"
               "等于绕过了『无可信证据则不进化』闸门")


def test_no_direct_pairs_zero_as_evidence():
  """不得再用 pairs[0] 直取作为进化依据（旧实现正是这么写的）。"""
  src = SRC.read_text(encoding="utf-8")
  assert "pairs[0] if pairs else" not in src, \
    "出现了 pairs[0] 直取写法，进化依据会退回单条且不过可信度筛选"
  assert "_select_evolution_evidence" in src, "证据筛选方法未被接线调用"


if __name__ == "__main__":
  import pytest
  sys.exit(pytest.main([__file__, "-q"]))
