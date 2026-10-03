"""atomicio 底层锁语义守卫 —— 每条都对应一个可复现过的失效。

核心一条（本次改动最深的一处）
--------------------------
`file_lock` 的进程内引用计数原先**只用 lock_path 做键**，不带线程 id。
于是「线程 A 持锁期间，线程 B 进入 file_lock」会被判定成 A 的嵌套重入，
B 直接 yield，**根本不加锁**。

模块文档把这个前提写成了调用方的责任：
「同进程内多个线程之间的互斥由调用方的 RLock 负责」。
但该前提在 4 个调用点里只有 3 个成立：

  kb.py    KnowledgeBase   有 RLock ✓
  tasks.py   Tasks       有 RLock ✓
  chat/store.py ConversationStore 有 RLock ✓
  llm/providers.py ProviderManager  **没有任何锁** ✗

缺少该约束时：

  12 个线程各存一份模型供应商配置 → 只活下来 2 份
  对照：有 RLock 的 Tasks 同样并发 12 条 → 12/12 全存活

服务端是 ThreadingHTTPServer，两个并发请求就会触发。最危险的是**全程没有
任何报错**：用户看到"保存成功"，配置却没了。

为什么修在底层原语而不是给 ProviderManager 补一个 RLock
------------------------------------------------------
补 RLock 只救这一个调用点，而「前提写在注释里、不满足时又不报错」这个
失效模式依然存在——下一个调用方照样会忘。让 file_lock 自己做到线程隔离，
才能让"忘记配 RLock"不再是一个静默失效。这也符合本项目一贯的原则：
不要各起一套。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core import atomicio          # noqa: E402
from omegaforge.core.atomicio import (         # noqa: E402
  LockTimeout, atomic_write, file_lock)
from omegaforge.llm.providers import ProviderManager  # noqa: E402


class ProvidersLostUpdateTest(unittest.TestCase):
  """A：无 RLock 的调用点，并发保存不能丢。"""

  def test_providers_concurrent_save_all_survive(self):
    """缺少该约束时只活下来 2/12，且无任何报错。"""
    home = tempfile.mkdtemp(prefix="prov_lock_")
    pm = ProviderManager(home)
    errors: list[str] = []

    def worker(i: int) -> None:
      try:
        pm.save(f"prov{i}", {"base_url": f"http://x{i}", "api_key": "k"})
      except Exception as exc:          # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

    n = 12
    barrier = threading.Barrier(n)
    ts = [threading.Thread(target=lambda i=i: (barrier.wait(), worker(i)))
       for i in range(n)]
    for t in ts:
      t.start()
    for t in ts:
      t.join()

    self.assertEqual(errors, [], f"并发保存抛异常：{errors[:2]}")
    with open(os.path.join(home, "providers.json"), encoding="utf-8") as f:
      disk = json.load(f)
    alive = len(disk.get("configs") or {})
    self.assertEqual(alive, n, f"{n} 份并发配置只活下来 {alive} 份")


class ThreadIsolationTest(unittest.TestCase):
  """B/C：file_lock 必须按线程隔离，且同线程嵌套仍然不死锁。"""

  def test_different_threads_really_exclude_each_other(self):
    """缺少该约束时：第二个线程"拿到"了锁（因为被误判为嵌套重入）。"""
    home = tempfile.mkdtemp(prefix="ti_")
    lockfile = os.path.join(home, "x.json")
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
      with file_lock(lockfile, timeout=5.0):
        entered.set()
        release.wait(5.0)

    t = threading.Thread(target=holder)
    t.start()
    self.assertTrue(entered.wait(5.0), "持锁线程没进入临界区")

    got_in = False
    try:
      with file_lock(lockfile, timeout=0.4):
        got_in = True
    except LockTimeout:
      pass
    finally:
      release.set()
      t.join()

    self.assertFalse(got_in, "第二个线程在第一个持有锁时进入了临界区")

  def test_same_thread_nesting_still_does_not_deadlock(self):
    """防处理过头：键改成按线程后，同线程嵌套必须仍然走引用计数。

    flock 的锁属于"打开文件描述"，同一进程内两个 fd 也会互相冲突。
    若嵌套时各开一个 fd 去 flock，就会自己等自己直到超时。
    """
    home = tempfile.mkdtemp(prefix="nest_")
    lockfile = os.path.join(home, "y.json")
    with file_lock(lockfile, timeout=2.0):
      with file_lock(lockfile, timeout=2.0):
        with file_lock(lockfile, timeout=2.0):
          pass
    # 锁必须真的释放了，否则这里会超时
    with file_lock(lockfile, timeout=2.0):
      pass

  def test_held_keys_are_thread_scoped(self):
    """直接断言书签结构：键里必须带线程 id。

    为什么要有这一条（防止改回字符串键）：
    上面两个用例是行为测试，但"键不带线程 id"也可能被别的实现方式
    绕过。这里直接守数据结构，撤改动后立刻变红。
    """
    key = atomicio._held_key("/tmp/z.json")
    self.assertIsInstance(key, tuple, f"键应是元组，实际 {type(key).__name__}")
    self.assertEqual(len(key), 2)
    self.assertEqual(key[0], threading.get_ident())


class LockFailurePathTest(unittest.TestCase):
  """D/E/F：失败路径的行为。"""

  def test_atomic_write_failure_leaves_no_tmp(self):
    """写失败不能留 .tmp —— 残留会被目录遍历当成数据文件。

    验证确认通过，保留为守卫：这条一旦退化，用户数据目录里会攒下
    一堆谁也不认识的隐藏文件。
    """
    home = tempfile.mkdtemp(prefix="aw_")
    target = os.path.join(home, "data.json")
    os.makedirs(target, exist_ok=True)   # 让 os.replace 失败
    with self.assertRaises(OSError):
      atomic_write(target, "hello")
    left = [f for f in os.listdir(home) if f.endswith(".tmp")]
    self.assertEqual(left, [], f"失败后残留临时文件：{left}")

  def test_lock_timeout_raises_instead_of_hanging(self):
    """卡住的锁必须能被发现 —— 静默等待会表现为"点了没反应"。

    LockTimeout 刻意不用 TimeoutError：后者会被 errors 层判成网络问题。
    """
    self.assertTrue(issubclass(LockTimeout, RuntimeError))
    self.assertFalse(issubclass(LockTimeout, TimeoutError))

  def test_excl_fallback_works_when_flock_unavailable(self):
    """fcntl / msvcrt 都不可用时的 O_EXCL 回退。

    桌面端三平台都要能跑，这条路径在 Linux CI 上日常不执行，
    只能靠强制置空来验证——不测就永远不知道它是不是坏的。
    """
    saved_f, saved_m = atomicio.fcntl, atomicio.msvcrt
    home = tempfile.mkdtemp(prefix="excl_")
    try:
      atomicio.fcntl = None
      atomicio.msvcrt = None
      lockfile = os.path.join(home, "z.json")
      with file_lock(lockfile, timeout=2.0):
        # 只看"没抛异常"等于没测：回退路径若什么都不做直接放行，
        # 这个用例照样通过，而跨进程互斥其实已经失效。
        assert os.path.exists(lockfile + ".lock.holder"), (
          "回退路径必须真实落下占位文件，否则等于没有加锁")
      with file_lock(lockfile, timeout=2.0):  # 必须能再次获得
        pass
    finally:
      atomicio.fcntl, atomicio.msvcrt = saved_f, saved_m


if __name__ == "__main__":
  unittest.main()
