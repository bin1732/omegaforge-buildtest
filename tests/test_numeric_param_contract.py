"""数值入参口径与脏事件容错的跨模块守卫。

本文件每一条都对应**可复现过**的问题，且都由一次跨模块同型扫描引出。
扫描给出的只是线索，判定真假必须验证——本次改动扫描命中的 4 处"返回侧截断"
里，3 处是误报（kb 有 _safe_limit、run.list 走 as_int、max_variants
只有内部默认），1 处（wiki.search）当前不可达。真正活的三处在下面。

1. **单条脏事件拖垮整个事件流**（omegaforge/core/run.py）
  `events()` 只兜了 JSONDecodeError，`int(d.get("seq", 0))` 没有兜。
  验证注入一行 `{"seq": "abc"}`：
    events(since=0) -> ValueError: invalid literal for int()...
    events(since=0) -> TypeError（seq 为 null 时）
  而 `ts` 异常反而没事——因为 events() 只转 seq。服务端
  `/api/runs/<id>/events` 正是调 run.events()，于是**一条坏行让任务
  详情打不开**。与用量账本是同一个形态：单条脏记录连累全局。

2. **外层裸 int() 把内层加固整个绕过**（agent/super_agent.py）
  `task_add` 内部早已写好 `_pri()`：验证 `_pri("high")=3`、`_pri([1])=3`、
  `_pri(10**20)=3`，健壮。但调用点写的是 `int(a.get("priority", 2))`，
  `int("high")` 会**先**抛 ValueError —— 永远走不到 _pri()。
  异常被 _call 压成「请求内容有误，请检查后重试」，不说是哪个参数，
  模型只能盲重试。加固不是缺失，是被绕过。

3. **MCP 有类型校验、无范围校验**（mcp_server.py）
  schema 层校验"是不是整数"，但 `limit` / `priority` 用裸 int()：
  -1 / 10**9 一路放行。distill 的参数早已用 as_int(minimum, maximum)，
  其余工具没接——同一次修复只覆盖了一部分调用点。

回退校验点（改坏后本文件必须变红）：
  A 撤掉 events() 的 _safe_seq    -> 第 1 组变红
  B 撤掉 Event.from_dict 的收敛    -> seq/ts 用例变红
  C agent 侧退回裸 int()       -> agent 用例变红
  D MCP 侧退回裸 int()        -> MCP 用例变红
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

_HERE = os.path.dirname(os.path.abspath(__file__))

from omegaforge.core import run as _run     # noqa: E402
from omegaforge.core.validate import UserError  # noqa: E402
from omegaforge.memory import tasks as _tasks   # noqa: E402


# ---------------------------------------------------------------- 1 脏事件

class RunEventToleranceTest(unittest.TestCase):
  """单条脏记录跳过，绝不连累全局。"""

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self._prev = os.environ.get("OMEGAFORGE_HOME")
    os.environ["OMEGAFORGE_HOME"] = self.tmp.name
    self.run = _run.RunStore().create("src")
    self.run.emit("start", {"x": 1})
    self.run.emit("phase", {"p": "ingest"})
    self.base = len(self.run.events(since=0))

  def _append(self, obj):
    with open(self.run.path, "a", encoding="utf-8") as f:
      f.write(json.dumps(obj, ensure_ascii=False) + "\n")

  def test_bad_seq_does_not_kill_whole_stream(self):
    """脏 seq 的行被跳过，其余事件照常返回——不是整条流崩掉。"""
    for bad in ({"seq": "abc", "ts": 1.0, "type": "log"},
          {"seq": None, "ts": 1.0, "type": "log"},
          {"seq": -9, "ts": 1.0, "type": "log"},
          {"seq": {"a": 1}, "ts": 1.0, "type": "log"}):
      self._append(bad)
      try:
        got = len(self.run.events(since=0))
      except Exception as e:            # noqa: BLE001
        self.fail(f"单条脏事件让整个事件流抛异常：{type(e).__name__}: {e}")
      # 脏行被丢弃，不应比基线多出任何东西
      self.assertEqual(got, self.base,
               f"脏 seq {bad['seq']!r} 不应进入事件流")

  def test_good_seq_still_passes(self):
    """防处理过头：合法 seq（含可收敛的浮点）不能被误丢。"""
    self._append({"seq": 99, "ts": 1.0, "type": "log", "message": "ok"})
    self._append({"seq": 100.0, "ts": 1.0, "type": "log", "message": "ok2"})
    got = self.run.events(since=0)
    self.assertEqual(len(got), self.base + 2)

  def test_since_filter_still_works(self):
    """防处理过头：since 过滤语义不能被收敛逻辑破坏。"""
    self.assertEqual(len(self.run.events(since=10**6)), 0)
    self.assertGreater(len(self.run.events(since=0)), 0)

  def test_from_dict_converges(self):
    """Event.from_dict 是公开 API，脏字段必须收敛而非抛异常。"""
    for d, seq in (({"seq": "abc", "ts": "xx"}, 0),
            ({"seq": None, "ts": None}, 0),
            ({"seq": "7", "ts": "1.5"}, 7)):
      e = _run.Event.from_dict(d)
      self.assertEqual(e.seq, seq, f"{d} -> seq={e.seq}")
    self.assertEqual(_run.Event.from_dict({"seq": 5, "ts": 1.5}).ts, 1.5)


# ------------------------------------------------- 2 内层加固 vs 外层裸转换

class InnerHardeningTest(unittest.TestCase):
  """外层 int() 不得先于内层收敛抛异常。"""

  def test_pri_inner_is_robust(self):
    """基线：内层 _pri 本身确实健壮（证明问题不在内层）。"""
    for raw in ("high", None, [1], {"a": 1}, -5, 10**20):
      self.assertIn(_tasks._pri(raw), (1, 2, 3))

  def test_raw_int_defeats_inner(self):
    """反例对照：裸 int() 会在到达 _pri 之前就抛——所以外层必须收敛。"""
    for raw in ("high", None, [1]):
      with self.assertRaises((TypeError, ValueError)):
        int(raw)


class AgentPriorityParamTest(unittest.TestCase):
  """agent 侧 priority 走 as_int，给模型可操作的中文提示。"""

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self._prev = os.environ.get("OMEGAFORGE_HOME")
    os.environ["OMEGAFORGE_HOME"] = self.tmp.name
    self.addCleanup(self._restore_home)

  def _restore_home(self):
    if self._prev is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = self._prev

  def test_agent_module_uses_as_int_not_bare(self):
    """接线守卫：源码里不得再出现裸 int(a.get('priority'。

    只测方法不够——接通断了照样全绿，这个坑本项目踩过多次。
    """
    src = open(os.path.join(ROOT, "omegaforge", "agent", "super_agent.py"),
          encoding="utf-8").read()
    self.assertNotIn('int(a.get("priority"', src)
    self.assertIn('as_int(a, "priority"', src)

  def test_as_int_message_is_actionable(self):
    """模型拿到的是哪个参数错，而不是一句泛化的"请检查后重试"。"""
    from omegaforge.core.validate import as_int
    with self.assertRaises(UserError) as ctx:
      as_int({"priority": "high"}, "priority", 2)
    msg = str(ctx.exception)
    self.assertIn("priority", msg, f"文案未点名参数：{msg}")
    self.assertNotIn("请稍后重试", msg)


# ------------------------------------------------------- 3 MCP 范围校验

class McpRangeTest(unittest.TestCase):
  """MCP 整数参数：schema 校验类型，as_int 校验范围，两层都要。"""

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self._prev = os.environ.get("OMEGAFORGE_HOME")
    os.environ["OMEGAFORGE_HOME"] = self.tmp.name
    self.addCleanup(self._restore_home)

  def _restore_home(self):
    if self._prev is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = self._prev

  def test_kb_search_limit_range(self):
    from omegaforge.mcp_server import McpCore
    from omegaforge.memory.kb import KnowledgeBase
    kb = KnowledgeBase()
    for i in range(5):
      kb.add(f"doc{i}", f"内容{i} 关键词", type="note")
    core = McpCore()
    ok = core.call_tool("kb_search", {"query": "关键词", "limit": 5})
    self.assertEqual(len(ok["results"]), 5)
    for bad in (-1, 0, 10**9):
      with self.assertRaises(UserError, msg=f"limit={bad} 应被范围校验拦下"):
        core.call_tool("kb_search", {"query": "关键词", "limit": bad})

  def test_task_add_priority_range(self):
    """放开作用域与权限后，priority 必须真正到达 as_int 的范围校验。"""
    from unittest import mock
    from omegaforge.tools.policy import Policy
    from omegaforge.tools.system_tools import MCP_SCOPE_DEFAULTS
    import omegaforge.mcp_server as ms

    Policy(self.tmp.name).set_mode("auto_edit")
    core = ms.McpCore()
    scope_all = {k: True for k in MCP_SCOPE_DEFAULTS}
    with mock.patch.object(ms.McpScope, "load", lambda self: scope_all):
      for good, want in ((1, 1), (2, 2), (3, 3)):
        r = core.call_tool("task_add", {"text": "x", "priority": good})
        self.assertEqual(r["priority"], want)
      for bad in (-5, 0, 10**20):
        with self.assertRaises(UserError,
                    msg=f"priority={bad} 应被范围校验拦下"):
          core.call_tool("task_add", {"text": "x", "priority": bad})

  def test_source_has_no_bare_int_for_these(self):
    """接线守卫：两处裸 int() 都必须已换成 as_int。"""
    src = open(os.path.join(ROOT, "omegaforge", "mcp_server.py"),
          encoding="utf-8").read()
    self.assertNotIn('limit=int(args.get("limit"', src)
    self.assertNotIn('int(args.get("priority"', src)

  def test_limits_is_single_source(self):
    """唯一真源：上界常量来自 limits.py，不得在调用点写死数字。"""
    from omegaforge.core import limits
    self.assertEqual(limits.PRIORITY_MAX, 3)
    self.assertEqual(limits.PRIORITY_MIN, 1)
    self.assertGreaterEqual(limits.SEARCH_LIMIT_MAX, limits.SEARCH_LIMIT_MIN)


if __name__ == "__main__":
  unittest.main(verbosity=2)
