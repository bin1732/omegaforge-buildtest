"""file_lock 的 _HELD 引用计数泄漏 —— 一次取不到 fd，互斥永久失效。

## 失效形态

`file_lock()` 在真正 open 锁文件**之前**，先把 `_HELD[key]` 设成 1
（acquired=True）。而 `os.makedirs()` / `os.open()` 这两行原本在
`try:` **之外**，于是它们一旦抛异常：

```python
  with _HELD_GUARD:
    ...
    else:
      _HELD[key] = 1     # ← 预占
      acquired = True
  ...
  d = os.path.dirname(lock_path)
  if d:
    os.makedirs(d, exist_ok=True)   # ← 抛异常（磁盘满 / 权限）
  fd = os.open(lock_path, ...)     # ← 抛异常
  try:                 # ← finally 在这里，管不到上面
    ...
  finally:
    _HELD[key] -= 1          # ← 永远不会执行
```

`_HELD[key]` **永久停留在 1**。此后本线程再调 `file_lock`：

```
n = _HELD.get(key, 0) → 1 → 判定为"重入" → acquired=False → 直接 yield
```

**根本不加锁，而调用方毫不知情。**

## 硬证据（跨进程验证）

另一进程用 `flock(LOCK_EX | LOCK_NB)` 探测同一把锁：

```
正常持锁      → 另一进程 OTHER-BLOCKED
一次 open 失败之后 → 另一进程 OTHER-GOT-LOCK  ← 互斥彻底失效
```

而且它是**永久**的：故障修好之后，该线程仍然永远不加锁。

## 为什么必须测行为而不是测字典

只断言 `_HELD` 为空是测实现细节——字典清空了不代表锁真的加上了。
本文件用**另一个真实进程去抢同一把锁**来证明互斥是否生效。
这是本项目反复强调的原则：只测内层不测接通，等于没测。

## 影响面

服务端每请求一个线程，泄漏随线程销毁；但 CLI / agent 主循环这类
**长期存活的线程**一旦泄漏，整个会话余下的跨进程互斥全失效
（4 个调用点：chat.store / llm.providers / memory.kb / memory.tasks）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core import atomicio as A  # noqa: E402
from omegaforge.core.atomicio import LockTimeout, file_lock  # noqa: E402


# 另一个进程：尝试用非阻塞 flock 抢同一把锁
_PROBE = textwrap.dedent('''
  import fcntl, os, sys
  fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("OTHER-GOT-LOCK")
  except OSError:
    print("OTHER-BLOCKED")
  os.close(fd)
''')


def _other_process_can_lock(lock_path: str) -> bool:
  """True = 另一进程抢到了锁（即本线程**没有**真正持锁）。"""
  with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
    f.write(_PROBE)
    script = f.name
  try:
    r = subprocess.run([sys.executable, script, lock_path],
              capture_output=True, text=True, timeout=60)
    return "OTHER-GOT-LOCK" in r.stdout
  finally:
    os.unlink(script)


class _FlakyOpen:
  """让 os.open 对 .lock 路径失败一次（模拟磁盘满 / 权限变更）。"""

  def __init__(self):
    self.real = os.open
    self.fail = True

  def __enter__(self):
    def flaky(path, flags, mode=0o777, *a, **k):
      if self.fail and str(path).endswith(".lock"):
        raise PermissionError("模拟磁盘满/权限故障")
      return self.real(path, flags, mode, *a, **k)
    os.open = flaky
    return self

  def __exit__(self, *exc):
    os.open = self.real
    self.fail = False
    return False


class HeldLeakTest(unittest.TestCase):
  """核心：一次取不到 fd，互斥必须不能永久失效。"""

  def setUp(self):
    A._HELD.clear()
    self.dir = tempfile.mkdtemp(prefix="lockleak_")
    self.path = os.path.join(self.dir, "data.json")

  def tearDown(self):
    A._HELD.clear()

  def test_baseline_other_process_blocked(self):
    """对照：正常持锁时，另一进程必须抢不到。"""
    with file_lock(self.path):
      self.assertFalse(
        _other_process_can_lock(self.path + ".lock"),
        "正常情况下另一进程应被挡住——若抢到，说明探测本身失效")

  def test_no_residue_after_open_failure(self):
    """缺少该约束时：open 失败后 _HELD 永久残留 1。"""
    with _FlakyOpen():
      with self.assertRaises(PermissionError):
        with file_lock(self.path):
          pass
    self.assertEqual(A._HELD, {}, f"_HELD 残留：{A._HELD}")

  def test_mutex_restored_after_failure(self):
    """核心行为守卫：故障恢复后，同一线程必须重新真正加锁。

    这是本文件最重要的一条——它测的是**行为**（另一进程能否抢到），
    不是字典内容。
    """
    with _FlakyOpen():
      with self.assertRaises(PermissionError):
        with file_lock(self.path):
          pass
    with file_lock(self.path):
      self.assertFalse(
        _other_process_can_lock(self.path + ".lock"),
        "故障恢复后互斥仍失效：file_lock 已退化为空操作")

  def test_exception_still_propagates(self):
    """修复不能顺手把异常吞掉——拿不到锁必须让调用方知道。"""
    with _FlakyOpen():
      with self.assertRaises(PermissionError):
        with file_lock(self.path):
          self.fail("不应进入临界区")


class NoOverFixTest(unittest.TestCase):
  """防处理过头：正常路径不能被改坏。"""

  def setUp(self):
    A._HELD.clear()
    self.dir = tempfile.mkdtemp(prefix="lockok_")
    self.path = os.path.join(self.dir, "data.json")

  def tearDown(self):
    A._HELD.clear()

  def test_nesting_count(self):
    """嵌套走引用计数：1 → 2 → 释放后回 0。"""
    key = (threading.get_ident(), self.path + ".lock")
    with file_lock(self.path):
      self.assertEqual(A._HELD.get(key), 1)
      with file_lock(self.path):
        self.assertEqual(A._HELD.get(key), 2)
    self.assertEqual(A._HELD.get(key), 0)

  def test_same_thread_sequential(self):
    """同一线程反复加锁释放，计数必须回到 0（不累积）。"""
    key = (threading.get_ident(), self.path + ".lock")
    for _ in range(5):
      with file_lock(self.path):
        pass
    self.assertEqual(A._HELD.get(key), 0)

  def test_other_thread_not_affected_by_this_thread(self):
    """线程隔离：本线程持锁期间，另一线程必须**真的去抢**锁。

    判定方式是"它会不会超时失败"，而不是"它拿没拿到"：
    若另一线程被误判成重入（线程隔离失效），它会**立即成功返回**；
    只有真的去 flock，才会在本线程持锁期间超时。

    （我第一版写成"主线程持锁并在 with 块内 join 子线程"，
    那是死锁：子线程默认等 10s 才超时，主线程却要等子线程结束
    才释放锁。是我的测试设计错误，不是产品问题。）
    """
    key_holder = []
    key_other = []

    def other():
      try:
        with file_lock(self.path, timeout=0.3):
          key_other.append("GOT")    # 不该发生
      except LockTimeout:
        key_other.append("TIMEOUT")    # 期望：真的去抢了

    with file_lock(self.path):
      key_holder.append(A._HELD.get(
        (threading.get_ident(), self.path + ".lock")))
      t = threading.Thread(target=other)
      t.start()
      t.join(timeout=15)
    self.assertEqual(key_holder, [1])
    self.assertEqual(key_other, ["TIMEOUT"],
             "另一线程未真正加锁（被误判为本线程的重入）")


class TimeoutTest(unittest.TestCase):
  """拿不到锁必须在有限时间内报错，不能静默等。"""

  def setUp(self):
    A._HELD.clear()
    self.dir = tempfile.mkdtemp(prefix="lockto_")
    self.path = os.path.join(self.dir, "data.json")

  def tearDown(self):
    A._HELD.clear()

  def test_timeout_is_bounded(self):
    done = threading.Event()

    def hold():
      with file_lock(self.path):
        done.set()
        time.sleep(2.5)

    t = threading.Thread(target=hold)
    t.start()
    self.assertTrue(done.wait(timeout=10))
    t0 = time.monotonic()
    with self.assertRaises(LockTimeout):
      with file_lock(self.path, timeout=0.5):
        self.fail("不该拿到锁")
    elapsed = time.monotonic() - t0
    self.assertLess(elapsed, 2.0, f"超时未生效，耗时 {elapsed:.2f}s")
    t.join(timeout=10)


if __name__ == "__main__":
  unittest.main()
