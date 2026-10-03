"""MCP 侧数值边界与跨版本比对守卫。

本文件每一条都对应一个可复现过的问题：

1. **数值边界：三个入口里只有 MCP 没接 limits.py。**
  缺少该约束时`call_tool("omegaforge_distill", ...)` 走的是裸
  `int(args.get(...))`，而 CLI 与 server 都用
  `as_int(..., minimum=, maximum=)`。同一批输入在两个入口被拒、在 MCP
  被放行 ——
    rounds=0 / gens=0 → 空转产物（0.00 分）却返回成功
    rounds=10**9    → 十亿轮评测
    budget=-1     → TokenBank 被压成 0
  而 MCP 是**唯一面向外部模型**的入口：调用方不是人，是别的 agent 按
  schema 猜的值，越界是常态而非意外。

2. **默认值也曾各自一套**：MCP 写死 rounds=3 / gens=1，limits.py 是
  6 / 3。同一个"蒸馏"动作在三个入口给出不同强度的结果。

3. **source 折成目录名时只替换 "/"，".." 原样保留**：source=".." 会把
  产物写进 mcp_runs 的父目录（用户数据根目录）。

4. **MCP 缺少该约束时没有 eval_set / ablate / compare**：外部 agent 通过 MCP
  蒸馏时每次都重新出题，既无法跨次比较，也无法做基因归因。

评测集只接受**内联用例**，不接受文件路径（HTTP 侧同一原则）：接受路径
等于让外部模型指定本机任意文件去读。
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core.limits import ( # noqa: E402
  MAX_BUDGET, MIN_BUDGET, ROUNDS_MAX, GENS_MAX,
  ROUNDS_DEFAULT, GENS_DEFAULT, DEFAULT_BUDGET)
from omegaforge.distill.engine import DistillEngine # noqa: E402
import omegaforge.mcp_server as M # noqa: E402
from omegaforge.core.errors import UserError # noqa: E402


class _RecEngine(DistillEngine):
  """只记录构造参数、不真跑蒸馏的替身。

  继承真类而不是另造：_normalize_eval_set 等静态校验仍在真实现上，
  否则守卫验的是替身自己的逻辑。
  """

  seen: dict = {}

  def __init__(self, llm, bank, **kw):
    _RecEngine.seen = {
      "rounds": kw.get("arena_rounds"),
      "gens": kw.get("max_generations"),
      "eval_set": kw.get("eval_set"),
      "budget": getattr(bank, "budget", None),
    }

  def distill(self, source, output_dir="output", task="", ablate=False,
        baseline_prompt=""):
    # 签名必须与真实现同步：真实现加了 baseline_提示词 之后，这里不跟
    # 上的话会抛 TypeError「got an unexpected keyword argument」——
    # 那是替身没跟上，不是实现有问题（替身与实现各说各话的典型形态）。
    _RecEngine.seen["ablate"] = ablate
    _RecEngine.seen["output_dir"] = output_dir
    _RecEngine.seen["baseline_prompt"] = baseline_prompt
    raise _Stop()


class _Stop(Exception):
  pass


class TestMcpNumericLimits(unittest.TestCase):
  """数值边界：与 CLI / server 共用 limits.py 这一唯一真源。"""

  def setUp(self):
    self._orig = M.DistillEngine
    M.DistillEngine = _RecEngine
    _RecEngine.seen = {}
    self.core = M.McpCore()

  def tearDown(self):
    M.DistillEngine = self._orig

  def _call(self, **args):
    args.setdefault("source", "agent-x")
    try:
      self.core.call_tool("omegaforge_distill", args)
    except _Stop:
      return "ok"
    return "ok"

  def _rejected(self, **args):
    with self.assertRaises(UserError) as ctx:
      self.core.call_tool("omegaforge_distill", args)
    return str(ctx.exception)

  def test_rounds_zero_rejected(self):
    """rounds=0 是空转配置：产物必然 0.00 分却返回成功。"""
    self.assertIn("评测轮数", self._rejected(source="s", rounds=0))

  def test_gens_zero_rejected(self):
    self.assertIn("进化代数", self._rejected(source="s", gens=0))

  def test_negative_rounds_rejected(self):
    self.assertIn("评测轮数", self._rejected(source="s", rounds=-5))

  def test_rounds_upper_bound(self):
    """上界缺失 = 请求放大：外部模型传 10**9 就是十亿轮评测。"""
    self.assertIn("评测轮数", self._rejected(source="s", rounds=10**9))

  def test_gens_upper_bound(self):
    self.assertIn("进化代数", self._rejected(source="s", gens=10**9))

  def test_budget_upper_and_lower_bound(self):
    self.assertIn("预算", self._rejected(source="s", budget=10**18))
    self.assertIn("预算", self._rejected(source="s", budget=-1))

  def test_boundary_values_accepted(self):
    """处理过头同样要防：恰好在边界内的值必须放行。"""
    self._call(source="s", rounds=ROUNDS_MAX, gens=GENS_MAX,
          budget=MAX_BUDGET)
    self.assertEqual(_RecEngine.seen["rounds"], ROUNDS_MAX)
    self.assertEqual(_RecEngine.seen["gens"], GENS_MAX)
    self._call(source="s", rounds=1, gens=1, budget=MIN_BUDGET)
    self.assertEqual(_RecEngine.seen["budget"], MIN_BUDGET)

  def test_defaults_match_limits(self):
    """默认值必须与唯一真源一致（修前 MCP 是 rounds=3 / gens=1）。"""
    self._call(source="s")
    self.assertEqual(_RecEngine.seen["rounds"], ROUNDS_DEFAULT)
    self.assertEqual(_RecEngine.seen["gens"], GENS_DEFAULT)
    self.assertEqual(_RecEngine.seen["budget"], DEFAULT_BUDGET)

  def test_non_integer_still_rejected_by_schema(self):
    """类型校验由 schema 负责，边界校验不该把它顶掉。"""
    self.assertIn("整数", self._rejected(source="s", rounds=1e9))


class TestMcpRunDirSafety(unittest.TestCase):
  def test_parent_traversal_flattened(self):
    self.assertEqual(M._run_dir(".."), "run")
    self.assertEqual(M._run_dir("../.."), "run")

  def test_separators_removed(self):
    for src in ("../../etc", "/etc/passwd", "a/b/c", "C:\\evil"):
      self.assertNotIn("/", M._run_dir(src))
      self.assertNotIn("\\", M._run_dir(src))
      self.assertNotIn("..", M._run_dir(src))

  def test_normal_name_preserved(self):
    self.assertEqual(M._run_dir("my-agent"), "my-agent")

  def test_output_stays_under_mcp_runs(self):
    """端到端：产物目录必须始终是 mcp_runs 的直接子目录。"""
    orig = M.DistillEngine
    M.DistillEngine = _RecEngine
    try:
      core = M.McpCore()
      for src in ("..", "../../etc", "/etc/passwd"):
        _RecEngine.seen = {}
        try:
          core.call_tool("omegaforge_distill", {"source": src})
        except _Stop:
          pass
        out = os.path.realpath(_RecEngine.seen["output_dir"])
        parent = os.path.realpath(
          os.path.join(core.kb.home, "mcp_runs"))
        self.assertEqual(os.path.dirname(out), parent, src)
    finally:
      M.DistillEngine = orig


class TestMcpEvalSetInline(unittest.TestCase):
  """评测集只接受内联用例：接受路径等于让外部模型指定本机文件。"""

  def setUp(self):
    self._orig = M.DistillEngine
    M.DistillEngine = _RecEngine
    _RecEngine.seen = {}
    self.core = M.McpCore()

  def tearDown(self):
    M.DistillEngine = self._orig

  def test_path_string_rejected(self):
    with self.assertRaises(UserError) as ctx:
      self.core.call_tool("omegaforge_distill",
                {"source": "s", "eval_set": "/etc/passwd"})
    self.assertIn("列表", str(ctx.exception))

  def test_inline_cases_reach_engine(self):
    cases = [{"input": "写一段摘要", "rubric": ["有要点"]}]
    try:
      self.core.call_tool("omegaforge_distill",
                {"source": "s", "eval_set": cases})
    except _Stop:
      pass
    self.assertEqual(len(_RecEngine.seen["eval_set"]), 1)
    self.assertEqual(_RecEngine.seen["eval_set"][0]["input"], "写一段摘要")

  def test_empty_set_not_silently_ignored(self):
    """传了空集却被当成"没传"，是又一种静默降级。"""
    with self.assertRaises(UserError) as ctx:
      self.core.call_tool("omegaforge_distill",
                {"source": "s", "eval_set": []})
    self.assertIn("空", str(ctx.exception))

  def test_missing_input_rejected_with_chinese(self):
    """缺题目内容必须被拒，且提示是中文（不得回显内部字段名）。"""
    with self.assertRaises(UserError) as ctx:
      self.core.call_tool("omegaforge_distill",
                {"source": "s", "eval_set": [{"rubric": ["x"]}]})
    msg = str(ctx.exception)
    self.assertIn("题目内容", msg, f"应点名缺少什么：{msg}")
    self.assertNotIn("input", msg, f"内部字段名不得外泄：{msg}")


class TestMcpBaselinePrompt(unittest.TestCase):
  """对照组 提示词：MCP 是外部模型唯一的补救入口，漏了就无处可填。

  验证：引擎的 build_baseline(sig) 从不转发 provided_prompt，
  三个入口也没有该入参——于是源材料里没有可提取 提示词 时结论必然
  不成立，而报告却让用户"提供源 Agent 原始 系统提示词"。
  """

  def setUp(self):
    self._orig = M.DistillEngine
    M.DistillEngine = _RecEngine
    _RecEngine.seen = {}
    self.core = M.McpCore()

  def tearDown(self):
    M.DistillEngine = self._orig

  def test_baseline_prompt_reaches_engine(self):
    p = "You are ArxivResearcher. " * 12
    try:
      self.core.call_tool("omegaforge_distill",
                {"source": "s", "baseline_prompt": p})
    except _Stop:
      pass
    self.assertEqual(_RecEngine.seen.get("baseline_prompt"), p)

  def test_absent_baseline_prompt_is_empty(self):
    """防处理过头：不传时不得凭空造一个出来。"""
    try:
      self.core.call_tool("omegaforge_distill", {"source": "s"})
    except _Stop:
      pass
    self.assertEqual(_RecEngine.seen.get("baseline_prompt"), "")

  def test_ablate_reaches_engine(self):
    try:
      self.core.call_tool("omegaforge_distill",
                {"source": "s", "ablate": True})
    except _Stop:
      pass
    self.assertTrue(_RecEngine.seen["ablate"])


class TestMcpCompare(unittest.TestCase):
  """跨版本比对：MCP 缺少该约束时完全没有这个工具。"""

  def setUp(self):
    self.core = M.McpCore()
    self.base = {"final_score": 5.0, "eval_set_fingerprint": "abc12345",
           "baseline_comparable": True, "claim_valid": True}

  def test_same_eval_set_gives_delta(self):
    r = self.core.call_tool("omegaforge_compare", {
      "prev": self.base, "curr": dict(self.base, final_score=8.4)})
    self.assertTrue(r["comparable"])
    self.assertAlmostEqual(r["delta"], 3.4)

  def test_different_eval_set_refuses_delta(self):
    r = self.core.call_tool("omegaforge_compare", {
      "prev": self.base,
      "curr": dict(self.base, eval_set_fingerprint="zzz99999")})
    self.assertFalse(r["comparable"])
    self.assertIsNone(r["delta"])
    self.assertIn("不同的评测集", r["reason"])

  def test_missing_fingerprint_refuses_delta(self):
    r = self.core.call_tool("omegaforge_compare", {
      "prev": {"final_score": 1.0}, "curr": self.base})
    self.assertFalse(r["comparable"])
    self.assertIn("指纹", r["reason"])

  def test_invalid_claim_refuses_delta(self):
    r = self.core.call_tool("omegaforge_compare", {
      "prev": dict(self.base, claim_valid=False), "curr": self.base})
    self.assertFalse(r["comparable"])
    self.assertIn("结论本身不成立", r["reason"])

  def test_compare_is_readonly_annotated(self):
    ann = M.McpCore.TOOL_ANNOTATIONS.get("omegaforge_compare") or {}
    self.assertTrue(ann.get("readOnlyHint"))
    self.assertFalse(ann.get("destructiveHint"))

  def test_compare_listed_in_public_tools(self):
    """接线守卫：只测方法不够，tools/list 里没有就等于外部模型看不见。"""
    names = [t["name"] for t in M.McpCore._public_tools()]
    self.assertIn("omegaforge_compare", names)


if __name__ == "__main__":
  unittest.main(verbosity=2)
