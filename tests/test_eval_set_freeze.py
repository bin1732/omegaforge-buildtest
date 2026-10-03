# -*- coding: utf-8 -*-
"""冻结评测集守卫：自进化"可评测"这条腿的落点。

为什么必须有它
--------------
缺少该约束时：考题每轮由被测方自己重新生成，于是——
 · 跨次运行分数不可比："这一版比上一版强"是拿两把不同的尺子量出来的；
 · 出题侧自证切不断：换裁判模型也修不掉"考什么、按什么标准判"这层偏差。

冻结评测集同时解决两件事：题不变 → 跨次可比；题与标准由使用者提供 →
出题侧闭环被切断。

本文件测真实 distill 主循环（不测内层方法就收工——验证接通漏了方法照样全绿）。
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import omegaforge.distill.engine as E # noqa: E402
from omegaforge.core.errors import UserError # noqa: E402
from omegaforge.core.budget import TokenBank # noqa: E402
from omegaforge.distill.engine import DistillEngine # noqa: E402
from omegaforge.distill.genome import Genome # noqa: E402
from omegaforge.domain.baseline import Baseline # noqa: E402

_REAL_LOADER = E.SourceAgentLoader


def _engine(eval_set=None, scores=(5.0,), baseline_score=9.0, max_generations=1):
  eng = DistillEngine.__new__(DistillEngine)
  eng.bank = TokenBank(9_000_000)
  eng.arena_rounds = 4
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
  eng.question_model = "fast-model"
  eng.rubric_source = "llm"
  eng._case_origin = {}
  eng.eval_set = list(eval_set) if eval_set else None
  eng._enter = lambda p: None
  eng._log = lambda m: None

  def fake_arena(g, cases, gen):
    # 沿用**真实的**来源统计，不在桩里另写一份：
    # 桩与实现各说各话的话，断言就成了自说自话（踩过）。
    eng._tally_case_origins(cases)
    d = scores[min(gen - 1, len(scores) - 1)]
    pair = {"case": "c1", "task": "t1",
        "answer_baseline": "b", "answer_distilled": "d",
        "score_baseline": baseline_score, "score_distilled": d,
        "judge_reason": "差距", "winner": "baseline",
        "debiased": True, "position_bias": 0.0,
        "orders_agree": True,
        "contamination": {"distilled": [], "baseline": []},
        "contaminated": False}
    eng.arena_cases = len(cases[:eng.arena_rounds])
    eng.arena_debiased = eng.arena_cases
    eng.arena_contaminated = 0
    return d, baseline_score, ["理由"], [pair]

  eng.run_arena = fake_arena
  eng.evolve_step = lambda g, t, r, a, b: g
  eng.step_extract = lambda sig: None
  eng.step_compress = lambda spec, lineage, fingerprint: Genome(
    name="x", mission_one_liner="m",
    source_fingerprint="f", lineage=lineage)
  eng.step_synthesize = lambda g: g
  def fake_gen_eval(g):
    # 与真实 step_gen_eval 一致：题与标准都来自被测方模型（llm/llm）。
    eng._case_origin["自动生成的题"] = ("llm", "llm")
    return [{"id": "auto1", "input": "自动生成的题",
         "rubric": ["模型自定标准"]}]

  eng.step_gen_eval = fake_gen_eval

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


def _run(eval_set=None, **kw):
  """跑 distill 并返回产物；评测集内容必须**在临时目录销毁前**读出。

  验证踩坑：直接把路径带出 TemporaryDirectory 上下文，目录已被删除，
  断言 os.path.exists 恒为 False——看起来像"没落盘"，其实是测试自己的错。
  """
  for eng in _engine(eval_set, **kw):
    with tempfile.TemporaryDirectory() as d:
      g, r = eng.distill("src", output_dir=d)
      path = os.path.join(d, "eval_set.json")
      text = (pathlib.Path(path).read_text(encoding="utf-8")
          if os.path.exists(path) else None)
      return g, r, text


# -- 落盘与复用 ------------------------------------------------------------

def test_eval_set_is_written_to_output():
  """本次改动使用的题必须落盘，否则下一轮无从复用。"""
  _g, r, text = _run()
  assert text is not None, "产物目录里没有 eval_set.json"
  data = json.loads(text)
  assert data["cases"][0]["input"] == "自动生成的题"
  assert r.eval_set_cases == 1
  assert r.eval_set_source == "generated"


def test_written_file_has_no_internal_fields():
  """落盘不能带内部字段——开发痕迹漏进用户可读文件是处理过的老问题。"""
  _g, _r, raw = _run()
  for leak in ("_origin", "_case_origin", "rubric_source"):
    assert leak not in raw, f"内部字段 {leak} 泄漏进评测集文件"
  keys = set(json.loads(raw)["cases"][0])
  assert keys == {"id", "input", "rubric"}, f"字段集不符：{keys}"


def test_roundtrip_reuse():
  """落盘 → 加载 → 内容一致。跨次比较的前提。"""
  _g, _r, text = _run()
  with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "eval_set.json")
    pathlib.Path(f).write_text(text, encoding="utf-8")
    loaded = DistillEngine.load_eval_set(f)
  assert loaded[0]["input"] == "自动生成的题"


# -- 切断出题侧自证 --------------------------------------------------------

def test_provided_eval_set_cuts_exam_self_authored():
  """沿用既有评测集 → 题与标准都不是被测方定的 → 出题侧闭环被切断。

  这是冻结评测集最值钱的收益：换裁判模型修不掉的那层偏差，
  靠"题由使用者给"才能真正切断。
  """
  _g, r, _p = _run(eval_set=[{"id": "u1", "input": "用户给的题",
                "rubric": ["人工标准"]}])
  assert r.eval_set_source == "provided"
  assert r.question_source == "user", \
    f"沿用评测集应记为 user，实际 {r.question_source}"
  assert r.exam_self_authored is False, \
    "题与标准都由使用者提供，不该再判为出题侧自证"


def test_generated_eval_set_still_flags_exam_self_authored():
  """防处理过头：自动生成时出题侧自证仍应存在，不能被一并洗掉。"""
  _g, r, _p = _run()
  assert r.eval_set_source == "generated"
  assert r.exam_self_authored is True, \
    "题由被测方出，不该因为有落盘就洗掉出题侧自证"


# -- 校验（畸形输入必须报错，不静默）----------------------------------------

def _expect_error(payload_fn, note=""):
  with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "b.json")
    pathlib.Path(f).write_text(json.dumps(payload_fn()), encoding="utf-8")
    try:
      DistillEngine.load_eval_set(f)
    except UserError:
      return
    except Exception as exc: # noqa: BLE001
      raise AssertionError(f"{note}：应抛 UserError，实际 {type(exc).__name__}")
    raise AssertionError(f"{note}：畸形评测集被静默通过")


def test_malformed_inputs_rejected():
  for fn, note in [
    (lambda: {}, "空对象"),
    (lambda: {"cases": []}, "空数组"),
    (lambda: [123], "非对象条目"),
    (lambda: [{"input": ""}], "缺 input"),
    (lambda: [{"input": "x", "rubric": "nope"}], "rubric 非数组"),
    (lambda: [{"input": "x" * 5000}], "input 过长"),
    (lambda: [{"input": "x", "rubric": ["r"] * 25}], "rubric 过多"),
    (lambda: [{"input": f"x{i}"} for i in range(101)], "条数超上限"),
  ]:
    _expect_error(fn, note)


def test_path_errors_are_actionable():
  with tempfile.TemporaryDirectory() as d:
    for bad, note in [(os.path.join(d, "nope.json"), "不存在"),
             (d, "目录"), ("", "空路径")]:
      try:
        DistillEngine.load_eval_set(bad)
      except UserError as exc:
        assert str(exc).strip(), f"{note}：提示不应为空"
        continue
      raise AssertionError(f"{note}：应抛 UserError")


def test_inline_payload_uses_same_validator():
  """HTTP 内联入参与文件加载共用同一套校验——分成两套必有入口漏项。"""
  try:
    DistillEngine._normalize_eval_set([{"input": ""}])
  except UserError:
    pass
  else:
    raise AssertionError("内联入参漏掉了 input 校验")
  ok = DistillEngine._normalize_eval_set([{"input": "x", "rubric": ["r"]}])
  assert ok[0]["input"] == "x"


if __name__ == "__main__":
  import pytest
  sys.exit(pytest.main([__file__, "-q"]))
