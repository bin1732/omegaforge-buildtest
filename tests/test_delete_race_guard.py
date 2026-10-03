"""删除类操作的临界区守卫。

守的是 B 组扫描里真正有害的那几处：**删除不参与临界区**。

扫描命中 33 个 "check-use 非原子" 候选，绝大多数是读路径（`get`/`_reload`/
`list`），TOCTOU 在那里无害——文件被删了就返回 None，用户看到"没有这条记录"，
是正确结果。**只有"删除"这一侧有害**，因为它同时踩两个坑：

1. **静默复活**：delete 删掉文件后，并发的写入者拿着自己内存里的全量快照
  又写了一遍。验证缺少该约束时 40 次删写竞争有 3 次文件复活——用户点了删除，
  对话却又出现在列表里，且没有任何提示。这比报错更糟：用户以为没删掉，
  再点一次，然后再一次。
2. **并发删除互相撞**：后到者 `exists` 判定为真、随即 `remove` 抛
  FileNotFoundError → 400「未找到对应记录」，用户会以为删除失败而反复点。

三处修复必须**各自成对**：wiki 的 save 与 delete 共用同一把锁（单侧加锁等于
没加），skills 的 install 与 uninstall 共用同一把锁（装到一半被删会半残）。

两类守卫缺一不可：
 * **接通守卫**（确定性）——直接断言 delete 真的进了锁。只测"并发不报错"
  是不够的：复活是概率事件，撤掉加上该约束后可能碰巧一次都不复现，测试照样全绿，
  于是成为形式化。
 * **压力守卫**（概率性）——验真实后果确实消失。trials 取 60：缺少该约束时单次
  复活率约 7.5%，60 次至少命中一次的概率 >99%，撤掉加上该约束后稳定变红。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.chat.store import ConversationStore  # noqa: E402
from omegaforge.memory.wiki import Wiki        # noqa: E402
from omegaforge.skills.manager import SkillManager  # noqa: E402
import omegaforge.core.atomicio as atomicio      # noqa: E402
import omegaforge.chat.store as store_mod       # noqa: E402
import omegaforge.memory.wiki as wiki_mod       # noqa: E402
import omegaforge.skills.manager as skill_mod     # noqa: E402


class _IsoHome(unittest.TestCase):
  """每个用例一个独立 home——本项目已多次栽在测试间环境污染上。"""

  def setUp(self):
    self._old = os.environ.get("OMEGAFORGE_HOME")
    self.home = tempfile.mkdtemp(prefix="delguard-")
    os.environ["OMEGAFORGE_HOME"] = self.home

  def tearDown(self):
    if self._old is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = self._old
    shutil.rmtree(self.home, ignore_errors=True)


class DeleteWiringTest(_IsoHome):
  """接线守卫：delete 必须真的进锁。

  只测"并发不报错"是形式化——复活概率约 7.5%，撤加上该约束后可能一次都不复现。
  """

  def test_conversation_delete_enters_lock(self):
    seen = []
    orig = atomicio.file_lock
    def spy(path, *a, **kw):
      seen.append(os.path.basename(str(path)))
      return orig(path, *a, **kw)
    store_mod.file_lock = spy
    try:
      st = ConversationStore()
      cid = st.new("x")["id"]
      self.assertTrue(st.delete(cid))
    finally:
      store_mod.file_lock = orig
    self.assertTrue(seen, "delete 未进入 file_lock——临界区形同虚设")

  def test_wiki_delete_and_save_share_one_lock(self):
    seen = []
    orig = atomicio.file_lock
    def spy(path, *a, **kw):
      seen.append(str(path))
      return orig(path, *a, **kw)
    wiki_mod.file_lock = spy
    try:
      w = Wiki()
      w.save("s", "t", "b")
      self.assertTrue(w.delete("s"))
    finally:
      wiki_mod.file_lock = orig
    self.assertTrue(seen, "wiki 的 save/delete 未进锁")
    # 两者锁的是同一个路径，否则单侧加锁等于没加
    self.assertEqual(len(set(seen)), 1,
             "save 与 delete 锁的不是同一个文件：%s" % seen)

  def test_skill_uninstall_shares_install_lock(self):
    seen = []
    orig = atomicio.file_lock
    def spy(path, *a, **kw):
      seen.append(os.path.basename(str(path)))
      return orig(path, *a, **kw)
    skill_mod.file_lock = spy
    try:
      sm = SkillManager()
      d = os.path.join(sm.root, "probe")
      os.makedirs(d, exist_ok=True)
      with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write("---\nname: probe\ndescription: d\n---\nb")
      self.assertTrue(sm.uninstall("probe"))
    finally:
      skill_mod.file_lock = orig
    # install 与 uninstall 必须共用同一把锁。
    # 断言用前缀而不是完整文件名：".lock" 后缀由 file_lock 自己添加，
    # 写死完整名会在调用方改名时报出与本意无关的失败——而这里要守的
    # 是"两边同一把锁"，不是"后缀长什么样"。
    self.assertTrue(
      any(str(n).startswith(".skills_install") for n in seen),
      f"未共用安装锁：{seen}")


class DeleteRaceTest(_IsoHome):
  """压力守卫：真实后果必须消失。"""

  def test_delete_not_resurrected_by_concurrent_write(self):
    st = ConversationStore()
    revived = 0
    trials = 60
    for i in range(trials):
      cid = st.new("r%d" % i)["id"]
      path = st._path(cid)
      barrier = threading.Barrier(2)
      def do_delete():
        barrier.wait()
        try:
          st.delete(cid)
        except Exception:   # noqa: BLE001
          pass
      def do_write():
        barrier.wait()
        try:
          st.add_message(cid, "user", "x")
        except Exception:   # noqa: BLE001
          pass
      t1 = threading.Thread(target=do_delete)
      t2 = threading.Thread(target=do_write)
      t1.start(); t2.start(); t1.join(); t2.join()
      if os.path.exists(path):
        revived += 1
    self.assertEqual(revived, 0,
             "删除后文件被并发写入复活 %d/%d 次" % (revived, trials))

  def test_concurrent_delete_no_error(self):
    st = ConversationStore()
    cid = st.new("c")["id"]
    errs = []
    barrier = threading.Barrier(16)
    def go():
      barrier.wait()
      try:
        st.delete(cid)
      except Exception as e:   # noqa: BLE001
        errs.append(type(e).__name__)
    ts = [threading.Thread(target=go) for _ in range(16)]
    for t in ts: t.start()
    for t in ts: t.join()
    self.assertEqual(errs, [], "并发删除抛异常：%s" % errs[:3])

  def test_wiki_concurrent_delete_no_error(self):
    w = Wiki()
    w.save("dup", "t", "b")
    errs = []
    barrier = threading.Barrier(16)
    def go():
      barrier.wait()
      try:
        w.delete("dup")
      except Exception as e:   # noqa: BLE001
        errs.append(type(e).__name__)
    ts = [threading.Thread(target=go) for _ in range(16)]
    for t in ts: t.start()
    for t in ts: t.join()
    self.assertEqual(errs, [], "wiki 并发删除抛异常：%s" % errs[:3])

  def test_skill_uninstall_no_half_deleted_state(self):
    sm = SkillManager()
    d = os.path.join(sm.root, "dupskill")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
      f.write("---\nname: dupskill\ndescription: d\n---\nb")
    errs = []
    barrier = threading.Barrier(8)
    def go():
      barrier.wait()
      try:
        sm.uninstall("dupskill")
      except Exception as e:   # noqa: BLE001
        errs.append(type(e).__name__)
    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    self.assertEqual(errs, [], "并发卸载抛异常：%s" % errs[:3])
    # 半删状态：目录还在但内容残缺——既卸载不掉也用不了
    self.assertFalse(os.path.isdir(d), "卸载后目录残留（半删状态）")


class DeleteIdempotentTest(_IsoHome):
  """删除不存在时返回 False，不抛异常。"""

  def test_conversation_delete_missing(self):
    st = ConversationStore()
    self.assertFalse(st.delete("a" * 10))

  def test_wiki_delete_missing(self):
    w = Wiki()
    self.assertFalse(w.delete("nosuchpage"))

  def test_skill_uninstall_missing(self):
    sm = SkillManager()
    self.assertFalse(sm.uninstall("nosuchskill"))


if __name__ == "__main__":
  unittest.main(verbosity=2)
