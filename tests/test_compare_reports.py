# -*- coding: utf-8 -*-
"""跨版本比对：给得出差值，也分得清为什么给不出。

用户点"比对两版"时，接口返回 200 且带着一段说明文字。只验状态码的话，
"两侧结论不成立"这种拒绝态同样判通过——于是验收全绿，而用户在任何输入下
都拿不到差值。因此这里同时验两条：条件齐备时必须给出 delta；缺哪一条
必须报出对应的那一条理由，而不是笼统拒绝。
"""
from __future__ import annotations

import pytest

from omegaforge.distill.engine import DistillEngine

FP = "a" * 16


def _report(**over) -> dict:
    base = {
        "final_score": 8.0,
        "eval_set_fingerprint": FP,
        "baseline_comparable": True,
        "claim_valid": True,
    }
    base.update(over)
    return base


def test_comparable_when_all_conditions_met():
    prev = _report(final_score=7.0)
    curr = _report(final_score=8.5)
    out = DistillEngine.compare_reports(prev, curr)
    assert out["comparable"] is True
    assert out["delta"] == 1.5
    assert not out.get("reason")


def test_score_drop_is_negative_delta():
    out = DistillEngine.compare_reports(_report(final_score=9.0),
                                        _report(final_score=6.0))
    assert out["comparable"] is True
    assert out["delta"] == -3.0


@pytest.mark.parametrize(
    "which,over_prev,over_curr,keyword",
    [
        ("指纹缺失", {"eval_set_fingerprint": ""}, {}, "评测集指纹"),
        ("指纹不同", {"eval_set_fingerprint": FP},
         {"eval_set_fingerprint": "b" * 16}, "不同的评测集"),
        ("上一版对照不可比", {"baseline_comparable": False}, {}, "上一版本"),
        ("本版对照不可比", {}, {"baseline_comparable": False}, "本版"),
        ("上一版结论不成立", {"claim_valid": False}, {}, "结论本身不成立"),
        ("本版结论不成立", {}, {"claim_valid": False}, "结论本身不成立"),
    ],
)
def test_each_rejection_has_its_own_reason(which, over_prev, over_curr,
                                           keyword):
    out = DistillEngine.compare_reports(_report(**over_prev),
                                        _report(**over_curr))
    assert out["comparable"] is False
    assert keyword in (out.get("reason") or ""), (
        f"{which} 应给出对应理由，实际 {out.get('reason')!r}")
    # 拒绝时 delta 键仍在但必须是 None：调用方若只看键存在与否，
    # 会把 None 当成一个具体差值渲染出来。
    assert out.get("delta") is None, "拒绝时不得给出具体差值"


def test_non_dict_input_is_rejected():
    out = DistillEngine.compare_reports(None, _report())
    assert out["comparable"] is False
    assert out.get("reason")
