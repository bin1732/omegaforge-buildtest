"""数据存储并发守卫。

守的是一件很底层、但后果最直接的事：**个人数据的写入必须既不损坏、也不丢失**。

出发点是一个反直觉的事实：本仓 11 处原子写都用 `path + ".tmp"` 这个**固定
临时文件名**。tmp+replace 本意是让读者看不到半成品，可一旦两个写入者共用
同一个 tmp，原子性就被 tmp 自己的名字毁掉了（详见 core/atomicio.py 的模块
文档）。服务端是 ThreadingHTTPServer，两个并发请求就够触发；桌面端 + CLI +
MCP 同时打开同一份数据是跨进程触发。

缺少该约束时：
 * 12 个并发 add，待办只活下来 1~2 条 —— 后写整体覆盖先写
 * 会话文件产出拼接的坏 JSON（JSONDecodeError: Extra data），
  此后**永久不可解析**，整段对话消失
 * json.dump 迭代途中字典被改：RuntimeError: dictionary changed
  size during iteration，从 _save() 冒泡成 500

这些守卫刻意走**真实并发**（真线程 / 真子进程），不走 mock：mock 掉写入
就等于把要测的东西测没了。回退校验 3 个校验点必须全部抓到。
"""

from __future__ import annotations

import json
import os
import unittest.mock as mock
import subprocess
import sys
import time
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core import atomicio as storage    # noqa: E402
from omegaforge.core.atomicio import (     # noqa: E402
  _replace, _unique_tmp, atomic_write, atomic_write_json, file_lock)
from omegaforge.memory.kb import KnowledgeBase  # noqa: E402
from omegaforge.memory.tasks import Tasks    # noqa: E402


class _HomeCase(unittest.TestCase):
  """每个用例独立 home，并在结束时还原环境。

  不还原会污染同批其它用例——本项目已多次栽在这里（全绿取决于执行顺序）。
  """

  def setUp(self):
    self._old = os.environ.get("OMEGAFORGE_HOME")
    self._tmp = tempfile.mkdtemp(prefix="conc_guard_")
    self.home = os.path.join(self._tmp, ".omegaforge")
    os.makedirs(self.home, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = self.home
    os.environ["MOCK"] = "1"

  def tearDown(self):
    if self._old is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = self._old


class AtomicWriteTest(_HomeCase):
  """原子写本身：临时文件名必须唯一，且不留残留。"""

  def test_01_tmp_name_is_unique_per_writer(self):
    p = os.path.join(self.home, "x.json")
    a = _unique_tmp(p)
    b = _unique_tmp(p)
    self.assertNotEqual(a, b, "两次写入不得共用同一个临时文件名")
    # pid 与线程 id 都要在里面：不同进程/线程之间必须错开
    self.assertIn(str(os.getpid()), a)
    self.assertIn(str(threading.get_ident()), a)
    # 固定名 path + ".tmp" 正是踩踏的根源，绝不能出现
    self.assertNotEqual(a, p + ".tmp")

  def test_02_concurrent_writes_never_produce_garbage(self):
    """并发写同一路径：结果必须是某一方的完整内容，绝不能是拼接体。"""
    p = os.path.join(self.home, "big.json")
    payloads = {i: json.dumps({"who": i, "pad": "z" * 200_000})
          for i in range(8)}
    barrier = threading.Barrier(len(payloads))
    errors = []

    def worker(i):
      try:
        barrier.wait()
        for _ in range(3):
          atomic_write(p, payloads[i])
      except Exception as exc:          # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in payloads]
    for t in ts:
      t.start()
    for t in ts:
      t.join()

    self.assertEqual(errors, [], f"并发写抛异常：{errors[:2]}")
    with open(p, encoding="utf-8") as f:
      raw = f.read()
    try:
      got = json.loads(raw)
    except json.JSONDecodeError as exc:
      self.fail(f"产出不可解析的文件（拼接体）：{exc}")
    self.assertIn(got["who"], list(payloads))
    self.assertEqual(raw, payloads[got["who"]], "内容被交错写入")

  def test_03_no_tmp_residue(self):
    """写完不能留 .tmp 垃圾——残留会被目录遍历当成数据文件。"""
    for i in range(5):
      atomic_write_json(os.path.join(self.home, f"r{i}.json"), {"i": i})
    leftovers = [f for f in os.listdir(self.home) if ".tmp" in f]
    self.assertEqual(leftovers, [], f"残留临时文件：{leftovers}")


class LostUpdateTest(_HomeCase):
  """丢更新：读-改-写必须合并别的写入者已落盘的内容。"""

  def test_04_tasks_concurrent_add_all_survive(self):
    t = Tasks(self.home)
    for i in range(200):         # 把文件养大，让写盘有可观测耗时
      t.add(f"seed-{i}")
    n = 12
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
      try:
        barrier.wait()
        t.add(f"new-{i}")
      except Exception as exc:      # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for x in ts:
      x.start()
    for x in ts:
      x.join()

    self.assertEqual(errors, [], f"并发 add 抛异常：{errors[:2]}")
    # 重新读盘：模拟进程重启后用户看到的结果
    with open(t.path, encoding="utf-8") as f:
      disk = json.load(f)
    alive = sum(1 for v in disk.values()
          if str(v.get("text", "")).startswith("new-"))
    self.assertEqual(alive, n, f"{n} 条并发新增只活下来 {alive} 条")

  def test_05_kb_concurrent_add_all_survive_and_no_runtime_error(self):
    kb = KnowledgeBase(self.home)
    for i in range(150):
      kb.add(f"seed-{i}", "x" * 1500)
    n = 12
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
      try:
        barrier.wait()
        kb.add(f"new-{i}", "y" * 1500)
      except Exception as exc:      # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for x in ts:
      x.start()
    for x in ts:
      x.join()

    self.assertEqual(errors, [], f"并发 add 抛异常：{errors[:2]}")
    for e in errors:
      self.assertNotIn("dictionary changed size", str(e))
    with open(kb.path, encoding="utf-8") as f:
      disk = json.load(f)
    alive = sum(1 for v in disk.values()
          if str(v.get("title", "")).startswith("new-"))
    self.assertEqual(alive, n, f"{n} 条并发新增只活下来 {alive} 条")

  def test_06_remember_nested_lock_does_not_deadlock(self):
    """remember() 内部调 add()：嵌套加锁不能自己等自己。

    flock 的锁属于打开文件描述，同一进程内两个 fd 也会互相冲突，所以
    file_lock 必须做进程内引用计数，否则这里必然超时。
    """
    kb = KnowledgeBase(self.home)
    kb.remember("记住这一条", tags=["a"])
    self.assertEqual(len(kb.all(type="memory")), 1)


class CrossProcessTest(_HomeCase):
  """跨进程：桌面端 / CLI / MCP 同时打开同一份数据的真实形态。"""

  # 关键点：worker 先**构造完成**（此时已把当时的磁盘内容读进内存），
  # 写完 ready 标记后等 go 信号，收到才写入。这样 4 个进程的内存快照
  # 一定都是"别的进程还没写"的旧状态——丢更新 100% 复现，不靠运气。
  # 去掉这道屏障，测试会变成"看机器快慢"，正是无效守卫守卫的常见来源。
  WORKER = r'''
import os, sys, time
sys.path.insert(0, {root!r})
home = sys.argv[1]; tag = sys.argv[2]
os.environ["OMEGAFORGE_HOME"] = home
os.environ["MOCK"] = "1"
from omegaforge.memory.kb import KnowledgeBase
from omegaforge.memory.tasks import Tasks
kb = KnowledgeBase(home); tk = Tasks(home)
open(os.path.join(home, "ready-" + tag), "w").close()
go = os.path.join(home, "GO")
for _ in range(2000):
  if os.path.exists(go):
    break
  time.sleep(0.01)
kb.add("doc-" + tag, "b-" + tag)
tk.add("task-" + tag)
'''

  def test_07_four_processes_no_lost_update(self):
    n = 4
    procs = []
    for i in range(n):
      procs.append(subprocess.Popen(
        [sys.executable, "-c",
         self.WORKER.format(root=ROOT), self.home, str(i)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=ROOT))
    # 等全部 worker 构造完毕（各自已持有旧快照），再统一放行
    for i in range(n):
      ready = os.path.join(self.home, f"ready-{i}")
      for _ in range(3000):
        if os.path.exists(ready):
          break
        time.sleep(0.01)
      self.assertTrue(os.path.exists(ready), f"worker {i} 未就绪")
    open(os.path.join(self.home, "GO"), "w").close()

    errs = []
    for p in procs:
      _, e = p.communicate()
      if e.strip():
        errs.append(e.strip()[-300:])
    self.assertEqual(errs, [], f"子进程报错：{errs[:1]}")

    with open(os.path.join(self.home, "kb.json"), encoding="utf-8") as f:
      kb = json.load(f)
    with open(os.path.join(self.home, "tasks.json"), encoding="utf-8") as f:
      tk = json.load(f)
    kb_alive = sum(1 for v in kb.values()
            if str(v.get("title", "")).startswith("doc-"))
    tk_alive = sum(1 for v in tk.values()
            if str(v.get("text", "")).startswith("task-"))
    self.assertEqual(kb_alive, n, f"{n} 个进程各写 1 条，只活下来 {kb_alive}")
    self.assertEqual(tk_alive, n, f"{n} 个进程各写 1 条，只活下来 {tk_alive}")


class ConversationRaceTest(_HomeCase):
  """同一会话并发追加：既要不丢消息，也不能产出坏文件。"""

  def test_08_concurrent_append_keeps_every_message(self):
    from omegaforge.chat.store import ConversationStore
    cs = ConversationStore(self.home)
    cid = cs.new("共享会话")["id"]
    n = 8
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
      try:
        barrier.wait()
        for k in range(4):
          cs.add_message(cid, "user", f"m-{i}-{k}")
      except Exception as exc:        # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for x in ts:
      x.start()
    for x in ts:
      x.join()

    self.assertEqual(errors, [], f"并发追加抛异常：{errors[:2]}")
    p = os.path.join(self.home, "conversations", cid + ".json")
    with open(p, encoding="utf-8") as f:
      raw = f.read()
    try:
      d = json.loads(raw)
    except json.JSONDecodeError as exc:
      self.fail(f"会话文件损坏（拼接体）：{exc}")
    self.assertEqual(len(d["messages"]), n * 4,
             f"期望 {n * 4} 条，实际 {len(d['messages'])} 条")


class FileLockTest(_HomeCase):
  def test_09_file_lock_is_reentrant_and_released(self):
    p = os.path.join(self.home, "guarded.json")
    with file_lock(p):
      with file_lock(p):     # 嵌套不得死锁
        atomic_write_json(p, {"ok": True})
    with file_lock(p):       # 锁必须真的释放了，否则这里会超时
      pass
    self.assertTrue(os.path.exists(p))


if __name__ == "__main__":
  unittest.main(verbosity=2)

class ReplaceRetryTest(_HomeCase):
  """Windows 上 os.replace 会瞬态失败，写入必须自己重试。

  在 Windows 上，替换一个正被别的句柄持有的文件会抛 PermissionError
  （WinError 5，共享冲突）——目标此刻正被另一个写入者替换、或被索引/查毒
  进程短暂打开都会触发。它不是权限问题，重放同一条语句就成功。

  不重试的后果落在用户身上：桌面端首要平台就是 Windows，保存待办/知识/
  会话时抛 PermissionError，"保存失败"而数据其实没问题。这类故障只在真机
  并发下现身，本地串行跑永远绿。

  重试挂在 `_replace` 的 `_impl` 上验，不去替换 `_replace` 本身：替换掉
  它就等于把被测的重试逻辑一起换掉了，测的成了替身。
  """

  def test_10_transient_permission_error_is_retried(self):
    calls = {"n": 0}

    def flaky(tmp, path):
      calls["n"] += 1
      if calls["n"] <= 2:
        raise PermissionError(5, "Access is denied")

    p = os.path.join(self.home, "retry.json")
    atomic_write(p, '{"ok": 1}')      # 先让目标存在
    _replace(p + ".tmp", p, retry_seconds=1.0, _impl=flaky)
    assert calls["n"] == 3, f"瞬态冲突后应继续尝试：{calls['n']}"

  def test_11_gives_up_and_leaves_no_tmp(self):
    def denied(tmp, path):
      raise PermissionError(5, "Access is denied")

    real = _replace

    def denied_replace(tmp, path, **kw):
      return real(tmp, path, retry_seconds=0.05, _impl=denied)

    p = os.path.join(self.home, "never.json")
    with mock.patch.object(storage, "_replace", denied_replace):
      with self.assertRaises(PermissionError):
        atomic_write(p, '{"ok": 1}')
    residue = [n for n in os.listdir(self.home) if n.endswith(".tmp")]
    assert residue == [], f"放弃后不得留下临时文件：{residue}"

  def test_12_other_errors_are_not_retried(self):
    """只有共享冲突重试；别的 OSError 必须原样抛出。

    把"磁盘满"也拿去重试，等于用一段无谓的等待把一个明确故障拖成卡顿，
    而抛出的还是同一个错——多等没有任何收益。
    """
    calls = {"n": 0}

    def boom(tmp, path):
      calls["n"] += 1
      raise OSError(28, "No space left on device")

    with self.assertRaises(OSError):
      _replace("a.tmp", "b.json", retry_seconds=1.0, _impl=boom)
    assert calls["n"] == 1, f"非共享冲突不应重试：{calls['n']}"
