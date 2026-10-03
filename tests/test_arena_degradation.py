"""竞技场判定降级路径守卫：单侧判定时分数不许对调。

为什么必须有这个文件
--------------------
双顺序去偏把判定拆成两次，A/B 含义相反：

  顺序1 A=对照组 B=蒸馏体
  顺序2 A=蒸馏体 B=对照组

两侧都成功时，取 (o1.a + o2.b)/2 与 (o1.b + o2.a)/2，交叉抵消位置偏好，
这个是对的。问题出在**只有一侧成功**的降级分支：原实现统一
`s_base, s_dist = one["a"], one["b"]`，而这一行只在顺序1下成立。

顺序1失败、顺序2成功时，两个分数整体对调。验证（judge 判蒸馏体 9.0、
对照组 1.0）：

  缺少该约束时 蒸馏体 1.0 / 对照组 9.0  ← 结论完全颠倒
  加上该约束后 蒸馏体 9.0 / 对照组 1.0

触发条件只是"第一次判定调用失败"——网络抖动、限流都会造成，远比
想象中常见。而它不报错、不崩溃，只在产物里静静记一个反的结论。
"蒸馏体更强"是产品核心主张，这条链上的数字不能被静默对调。

本文件全部用替身 judge 构造**确定**的分数，不依赖真实模型。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.core.budget import TokenBank     # noqa: E402
from omegaforge.distill.engine import DistillEngine  # noqa: E402
from omegaforge.distill.genome import Genome     # noqa: E402
from omegaforge.domain.baseline import Baseline    # noqa: E402


class _L:
  model_main = model_fast = model_judge = "fake"

  def chat(self, system, user, model=None, temperature=0.4,
       max_tokens=2048, json_mode=False):
    return type("R", (), {"text": "answer", "model": "f",
               "prompt_tokens": 1, "completion_tokens": 1})()

  def chat_messages(self, messages, model=None, temperature=0.4,
           max_tokens=2048, json_mode=False):
    return self.chat("", "")


def _engine(judge):
  g = Genome(name="D", mission_one_liner="m", source_fingerprint="f",
        system_prompt="sp")
  eng = DistillEngine(_L(), TokenBank(500000), verbose=False)
  eng.baseline = Baseline(kind="source_prompt", system_prompt="bp",
              note="src")
  eng._judge_once = judge
  return eng, g


# 替身 judge 的语义与真实一致：
#  _judge_once(base, ans_a, ans_b) —— A 位放什么，a 就是它的分。
# 顺序1 调用为 (baseline, distilled)；顺序2 调用为 (distilled, baseline)。
# 让"蒸馏体恒为 9.0、对照组恒为 1.0"是唯一真实，看产物是否还认得出来。
DIST, BASE = 9.0, 1.0


def _judge_by_position(script):
  """script: 每次调用返回 None 或 {'a':..., 'b':...}，按调用次序取。"""
  calls = {"n": 0}

  def _j(base_prompt, ans_a, ans_b):
    i = calls["n"]
    calls["n"] += 1
    r = script[min(i, len(script) - 1)]
    return r
  return _j


class TestDegradedScoring(unittest.TestCase):

  def _run(self, script):
    eng, g = _engine(_judge_by_position(script))
    d, b, reasons, pairs = eng.run_arena(
      g, [{"id": "c1", "input": "t", "rubric": ["r"]}], 0)
    return d, b, pairs[0]

  def test_order2_only_not_inverted(self):
    """顺序1失败、顺序2成功：A 位是蒸馏体，映射必须反过来。

    这是验证抓到的那个 bug——缺少该约束时这里会得到 蒸馏体1.0/对照组9.0。
    """
    d, b, p = self._run([
      None,                  # 顺序1 判定失败
      {"a": DIST, "b": BASE, "winner": "A", "reason": "A 更好"},
    ])
    self.assertAlmostEqual(d, DIST, places=3,
                msg="顺序2单独成立时蒸馏体分数被换成了对照组的")
    self.assertAlmostEqual(b, BASE, places=3,
                msg="顺序2单独成立时对照组分数被换成了蒸馏体的")
    self.assertFalse(p["debiased"], "未去偏却被标成已去偏")

  def test_order1_only_unchanged(self):
    """顺序1成功、顺序2失败：原映射就对，不能被改坏。"""
    d, b, _ = self._run([
      {"a": BASE, "b": DIST, "winner": "B", "reason": "B 更好"},
      None,
    ])
    self.assertAlmostEqual(d, DIST, places=3)
    self.assertAlmostEqual(b, BASE, places=3)

  def test_both_fail_no_crash(self):
    """两侧都失败：不得崩溃，也不得凭空造分。"""
    d, b, p = self._run([None, None])
    self.assertEqual((d, b), (0.0, 0.0),
             "两侧判定都失败却给出了非零分数")
    self.assertFalse(p["debiased"])

  def test_both_succeed_still_debiased(self):
    """两侧都成功：去偏路径不受本次修改影响（防改坏既有逻辑）。"""
    d, b, p = self._run([
      {"a": BASE, "b": DIST, "winner": "B", "reason": "r"},
      {"a": DIST, "b": BASE, "winner": "A", "reason": "r"},
    ])
    self.assertTrue(p["debiased"])
    self.assertAlmostEqual(d, DIST, places=3)
    self.assertAlmostEqual(b, BASE, places=3)
    self.assertAlmostEqual(p["position_bias"], 0.0, places=3)

  def test_degraded_not_presented_as_debiased(self):
    """降级结果必须带'未去偏'标记，不能冒充去偏结论。"""
    _, _, p = self._run([
      None,
      {"a": DIST, "b": BASE, "winner": "A", "reason": "r"},
    ])
    self.assertFalse(p["debiased"])
    self.assertIn(p["winner"], ("undetermined", "disagreement", "tie"),
           "单侧判定却给出了确定的胜负")


if __name__ == "__main__":
  unittest.main(verbosity=2)
