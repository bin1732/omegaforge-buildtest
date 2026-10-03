# -*- coding: utf-8 -*-
"""跨版本比对守卫：这一版比上一版强了多少、依据哪套题。

为什么必须有它
--------------
冻结评测集让"跨次可比"成为可能，但**可能**不等于**已经做到**：
最难发现的错误不是算错，而是把**不可比的两个数相减**——题不同、
对照组不可比、任一侧结论不成立，delta 都是一个看似精确、实则无意义的数。

另一条：结论成立 / 裁判自证 /
结论成立、裁判自证、出题侧自证三项是 property，**不在 `__dict__` 里**，而 `to_json`
原本序列化 `self.__dict__` → 产物 report.json 里三项**全部丢失**。
后果不只是"少几个字段"：比对读历史报告时 结论是否成立 恒为 None →
永远判定"有一侧的结论本身不成立" → 跨版本比对整体不可用，
而且用户拿产物根本无从复核结论可信度。

本文件走真实 distill + 真实落盘，不测内层方法就收工。
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import omegaforge.distill.engine as E # noqa: E402
from omegaforge.core.budget import TokenBank # noqa: E402
from omegaforge.distill.engine import DistillEngine # noqa: E402

_CASES = [{"id": "c1", "input": "把这段文本按句切分",
      "rubric": ["正确性"]}]
_SRC = ("You are a helpful assistant that writes clean Python code. "
    "Always explain your reasoning step by step and return only "
    "the requested output.")


def _src_file(d):
  p = os.path.join(d, "src.py")
  pathlib.Path(p).write_text(_SRC, encoding="utf-8")
  return p


def _es_file(d):
  p = os.path.join(d, "es.json")
  pathlib.Path(p).write_text(json.dumps({"cases": _CASES},
                     ensure_ascii=False), encoding="utf-8")
  return p


def _distill(out_dir, eval_set=None, judge="other-judge-model"):
  """跑一次真实 distill（mock LLM），返回 report dict。"""
  os.environ["OMEGAFORGE_MODEL_JUDGE"] = judge
  eng = DistillEngine(E.LLMClient(), TokenBank(9_000_000),
            arena_rounds=1, max_generations=1,
            verbose=False, eval_set=eval_set)
  _g, r = eng.distill(_src_file(os.path.dirname(os.path.abspath(out_dir))),
            output_dir=out_dir)
  return r, json.loads(open(os.path.join(out_dir, "report.json"),
               encoding="utf-8").read())


# -- 指纹性质 --------------------------------------------------------------

def test_fingerprint_is_order_and_id_independent():
  """顺序与编号都无关：它们不代表换了一套题。"""
  a = [{"input": "甲", "rubric": ["r1"]}, {"input": "乙"}]
  assert DistillEngine.eval_set_fingerprint(a) == \
    DistillEngine.eval_set_fingerprint(list(reversed(a)))
  assert DistillEngine.eval_set_fingerprint(a) == \
    DistillEngine.eval_set_fingerprint(
      [{"id": "z", "input": "甲", "rubric": ["r1"]},
       {"id": "y", "input": "乙"}])


def test_fingerprint_changes_when_question_changes():
  """题变一个字就必须不可比——这是跨版本比对的立身之本。"""
  a = [{"input": "甲", "rubric": ["r1"]}]
  b = [{"input": "甲!", "rubric": ["r1"]}]
  assert DistillEngine.eval_set_fingerprint(a) != \
    DistillEngine.eval_set_fingerprint(b)
  # rubric 变了同样不可比：尺子变了
  c = [{"input": "甲", "rubric": ["r2"]}]
  assert DistillEngine.eval_set_fingerprint(a) != \
    DistillEngine.eval_set_fingerprint(c)


# -- 落盘：property 不能丢（真 bug）--------------------------------------

def test_report_persists_derived_trust_fields():
  """产物 report.json 必须带结论可信度三件套。

  验证缺少该约束时：三项是 property，不在 __dict__ 里，to_json 落盘全丢，
  report.json 里 结论是否成立 恒为 None。
  """
  with tempfile.TemporaryDirectory() as d:
    _r, disk = _distill(os.path.join(d, "o"), eval_set=_CASES)
    for k in ("claim_valid", "self_certified", "exam_self_authored"):
      assert k in disk, f"落盘丢失字段 {k}"
      assert isinstance(disk[k], bool), f"{k} 应为布尔，实际 {disk[k]!r}"


def test_to_dict_matches_properties():
  with tempfile.TemporaryDirectory() as d:
    r, disk = _distill(os.path.join(d, "o"), eval_set=_CASES)
    assert disk["claim_valid"] == r.claim_valid
    assert disk["self_certified"] == r.self_certified
    assert disk["exam_self_authored"] == r.exam_self_authored


def test_fingerprint_persisted_and_matches():
  with tempfile.TemporaryDirectory() as d:
    _r, disk = _distill(os.path.join(d, "o"), eval_set=_CASES)
    assert disk["eval_set_fingerprint"] == \
      DistillEngine.eval_set_fingerprint(_CASES)
    assert disk["eval_set_cases"] == 1


# -- 比对规则 --------------------------------------------------------------

def _base(**kw):
  d = {"final_score": 5.0, "baseline_comparable": True,
     "claim_valid": True,
     "eval_set_fingerprint": DistillEngine.eval_set_fingerprint(_CASES)}
  d.update(kw)
  return d


def test_comparable_gives_delta():
  r = DistillEngine.compare_reports(_base(final_score=5.0),
                   _base(final_score=8.4))
  assert r["comparable"] is True and r["delta"] == 3.4


def test_delta_can_be_negative():
  r = DistillEngine.compare_reports(_base(final_score=8.4),
                   _base(final_score=5.0))
  assert r["comparable"] is True and r["delta"] == -3.4


def test_different_eval_set_is_not_comparable():
  """题不同 → 拒绝给 delta。给一个精确数字是最坏的结果。"""
  r = DistillEngine.compare_reports(_base(), _base(
    eval_set_fingerprint="deadbeefdeadbeef"))
  assert r["comparable"] is False and r["delta"] is None
  assert "不可比" in r["reason"]


def test_missing_fingerprint_is_not_comparable():
  r = DistillEngine.compare_reports(_base(eval_set_fingerprint=""),
                   _base())
  assert r["comparable"] is False and r["delta"] is None


def test_incomparable_baseline_blocks_delta():
  for side in ("prev", "curr"):
    p = _base() if side == "curr" else _base(baseline_comparable=False)
    c = _base(baseline_comparable=False) if side == "curr" else _base()
    r = DistillEngine.compare_reports(p, c)
    assert r["comparable"] is False, f"{side} 侧对照组不可比应拦下"


def test_invalid_claim_blocks_delta():
  r = DistillEngine.compare_reports(_base(claim_valid=False), _base())
  assert r["comparable"] is False
  assert "结论本身不成立" in r["reason"]


def test_missing_history_is_reported_not_crashed():
  for prev in (None, {}, []):
    r = DistillEngine.compare_reports(prev, _base())
    assert r["comparable"] is False and r["reason"]


def test_reason_is_non_empty_chinese():
  """理由是要直接给用户看的，不能是异常名或空串。"""
  r = DistillEngine.compare_reports(_base(), _base(
    eval_set_fingerprint="deadbeefdeadbeef"))
  assert r["reason"].strip() and not r["reason"].isascii()


if __name__ == "__main__":
  import pytest
  sys.exit(pytest.main([__file__, "-q"]))
