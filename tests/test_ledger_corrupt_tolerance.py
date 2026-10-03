"""损坏账本的容错守卫 —— 同一类失效在两个文件里各演了一遍。

共同的根因
----------
`UnicodeDecodeError` 是 **ValueError 的子类，不是 OSError**。
所以「读盘时 `except OSError`」这种写法**接不住编码错误**。

两处验证（都是端到端真实调用，不是推断）：

1. `llm/usage.py` UsageStore._entries —— 账本是一个坏字节时：

    GET /api/usage  → 400「操作失败，请稍后重试」
    GET /api/kb/list → 200 正常

  用量页永久打不开，而**用户没有任何清理入口**——用量页本身就是那个
  入口，它挂了就无处可清。这正是该文件头 docstring 自己警告的场景
  （"它一崩……等于用量页永久打不开"），但守卫只写了一半。

2. `tools/ledger.py` ContextLedger._load —— 影响更广：

    ContextLedger.add() 抛 UnicodeDecodeError
    而 add() 在**工具执行路径上**被调用（system_tools.py exec 分支，
    每条命令都记一笔）
    → 一个损坏的账本文件让**所有工具调用全挂**

  该文件自己的取舍写得清楚："聚合状态写不进去不该阻断主流程……绝不能
  让工具不可用"，但这个取舍只做在写入侧（_save），读取侧漏了。

为什么修在读取侧而不是"让用户去删文件"
--------------------------------------
两处损坏的文件**都没有任何清理入口**（用量页挂了就无法清理账本，
工具挂了就无法清理 ledger）。所以读取侧必须自愈。

为什么 usage 改成逐字节读再单行解码
----------------------------------
用 `except ValueError` 整体兜住也能不崩，但那会在坏行处**中断迭代**，
坏行之后的所有记录一起丢失——与"单条脏记录跳过，绝不连全局"相反。
逐行 decode(errors="replace") 才能让坏行之后的记录全部保留。
（本文件有专门的用例守这一条。）
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

from omegaforge.llm.usage import UsageStore     # noqa: E402
from omegaforge.tools.ledger import ContextLedger  # noqa: E402


class UsageCorruptTest(unittest.TestCase):
  """用量账本：坏行跳过，其余全部保留。"""

  def _write(self, raw: bytes) -> UsageStore:
    home = tempfile.mkdtemp(prefix="usage_corrupt_")
    s = UsageStore(home)
    with open(s.path, "wb") as f:
      f.write(raw)
    return s

  def test_non_utf8_does_not_break_summary(self):
    """缺少该约束时：UnicodeDecodeError 冒泡 → /api/usage 400。"""
    s = self._write(
      b'{"ts":1,"phase":"a","model":"m","tokens":100}\n'
      b'\xff\xfe\x00corrupt\n'
      b'{"ts":1,"phase":"b","model":"m","tokens":7}\n')
    try:
      r = s.summary()
    except Exception as exc:           # noqa: BLE001
      self.fail(f"非 UTF-8 账本让 summary 抛异常：{type(exc).__name__}: {exc}")
    self.assertEqual(r["total"], 107)
    self.assertEqual(r["entries"], 2)
    self.assertEqual(r["by_phase"], {"a": 100, "b": 7})

  def test_records_after_bad_line_survive(self):
    """防处理过头：坏行**之后**的记录必须仍在。

    若改成 `except ValueError` 整体兜住，坏行处就中断迭代了，
    后面 2 条会一起丢——这条专门守它。
    """
    parts = []
    for i in range(5):
      parts.append(json.dumps(
        {"ts": 1, "phase": f"p{i}", "model": "m", "tokens": 10}).encode())
      if i == 2:
        parts.append(b"\xc3\x28 bad utf8\n")
    s = self._write(b"\n".join(parts) + b"\n")
    r = s.summary()
    self.assertEqual(r["entries"], 5, f"坏行后记录丢失，只剩 {r['entries']} 条")
    self.assertEqual(r["total"], 50)

  def test_all_lines_corrupt_yields_empty_not_error(self):
    """全是坏行时返回空视图，而不是 500。"""
    s = self._write(b"\xff\xfe\x00\n\xc3\x28\n")
    r = s.summary()
    self.assertEqual(r["total"], 0)
    self.assertEqual(r["entries"], 0)

  def test_healthy_ledger_unaffected(self):
    """防处理过头：正常账本照常工作。"""
    home = tempfile.mkdtemp(prefix="usage_ok_")
    s = UsageStore(home)
    s.record("gen", "m", 100, 60, 40)
    s.record("eval", "m", 50, 30, 20)
    r = s.summary()
    self.assertEqual(r["total"], 150)
    self.assertEqual(r["entries"], 2)


class LedgerCorruptTest(unittest.TestCase):
  """上下文账本：损坏必须退化为"无历史"，绝不阻断工具调用。"""

  def _corrupt(self) -> ContextLedger:
    home = tempfile.mkdtemp(prefix="ledger_corrupt_")
    lg = ContextLedger(home)
    lg.path.parent.mkdir(parents=True, exist_ok=True)
    lg.path.write_bytes(b'[{"kind":"write"}]\n\xff\xfe bad\n')
    return lg

  def test_corrupt_ledger_does_not_block_add(self):
    """缺少该约束时：add() 抛 UnicodeDecodeError → 所有工具调用连带失败。

    add() 在工具执行路径上被调用（system_tools.py exec 分支），
    所以这里失败等于工具全挂。
    """
    lg = self._corrupt()
    try:
      lg.add("exec", "x.sh", "echo hi", risk=True)
    except Exception as exc:           # noqa: BLE001
      self.fail(f"损坏账本阻断了工具记账：{type(exc).__name__}: {exc}")
    self.assertEqual(lg.stats()["execs"], 1)

  def test_corrupt_ledger_stats_still_works(self):
    """损坏账本退化为"无历史"——entries 为 0，但不抛异常。

    注意期望值就是 0：账本整体读不出来时无法逐条容错，只能全部当作
    没有历史。这是有意的取舍（见 ledger._load 的 docstring）：
    宁可失去累积风险判定能力，也不能让工具全部不可用。

    （我第一版把这里写成期望 1，是我自己的断言错误——上一个用例里
    先 add 了一条才会是 1。断言自洽性存疑时先怀疑断言。）
    """
    lg = self._corrupt()
    st = lg.stats()
    self.assertEqual(st["entries"], 0)

  def test_healthy_ledger_unaffected(self):
    """防处理过头：正常账本的历史必须照常读出来。"""
    home = tempfile.mkdtemp(prefix="ledger_ok_")
    lg = ContextLedger(home)
    lg.add("exec", "a.sh", "ls", risk=True)
    lg.add("read", "b.txt", "x", risk=False)
    st = lg.stats()
    self.assertEqual(st["entries"], 2)
    self.assertEqual(st["execs"], 1)
    self.assertEqual(st["risk_entries"], 1)


class DecodeErrorClassTest(unittest.TestCase):
  """守住根因本身：`UnicodeDecodeError` 不是 OSError。

  为什么要有这一条（防止将来有人"简化"成只兜 OSError）：
  上面两组是行为测试，但根因是继承关系。直接断言继承链，
  撤改动后立刻变红，而且能让改代码的人当场看见原因。
  """

  def test_unicode_decode_error_is_not_os_error(self):
    self.assertTrue(issubclass(UnicodeDecodeError, ValueError))
    self.assertFalse(issubclass(UnicodeDecodeError, OSError))
    # json.JSONDecodeError 同样是 ValueError 子类
    self.assertTrue(issubclass(json.JSONDecodeError, ValueError))


if __name__ == "__main__":
  unittest.main()
