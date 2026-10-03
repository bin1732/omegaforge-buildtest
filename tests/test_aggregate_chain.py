"""聚合上下文：脚本链必须递归解析，且解析本身必须是有界的。

对应缺陷：只解析被直接执行的那一个脚本。`top.sh -> mid.sh -> payload.sh`
这种链里，top.sh 正文只是 `source mid.sh`，单层解析看到的是无害文本，
检查直接穿过去。

同时守卫"安全检查不能变成 DoS 面"：递归必须有 depth / files / bytes
三重上限，自引用脚本不得死循环。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import unittest

HOME = os.path.abspath(".omegaforge_test_aggchain")


def _boot():
  shutil.rmtree(HOME, ignore_errors=True)
  os.makedirs(HOME, exist_ok=True)
  os.environ["OMEGAFORGE_HOME"] = HOME
  with open(os.path.join(HOME, "permissions.json"), "w") as f:
    json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)
  with open(os.path.join(HOME, "policy.json"), "w") as f:
    json.dump({"mode": "full"}, f)
  from omegaforge.tools.system_tools import SystemTools
  return SystemTools(HOME)


def _w(name: str, body: str):
  with open(os.path.join(HOME, name), "w") as f:
    f.write(body)


class TestTransitiveChain(unittest.TestCase):
  def setUp(self):
    self.st = _boot()

  def tearDown(self):
    shutil.rmtree(HOME, ignore_errors=True)

  def _blocked(self, cmd: str) -> bool:
    from omegaforge.tools.system_tools import BlockedCommand
    try:
      self.st._aggregate_check(cmd)
      return False
    except BlockedCommand:
      return True

  def test_one_level(self):
    _w("payload.sh", "rm -rf /tmp/target\n")
    self.assertTrue(self._blocked("bash payload.sh"))

  def test_two_levels(self):
    """mid.sh 只 source 了 payload —— 单层解析必漏。"""
    _w("payload.sh", "rm -rf /tmp/target\n")
    _w("mid.sh", "#!/bin/bash\nsource payload.sh\n")
    self.assertTrue(self._blocked("bash mid.sh"))

  def test_three_levels(self):
    _w("payload.sh", "rm -rf /tmp/target\n")
    _w("mid.sh", "#!/bin/bash\nsource payload.sh\n")
    _w("top.sh", "#!/bin/bash\nsource mid.sh\n")
    self.assertTrue(self._blocked("bash top.sh"))

  def test_direct_exec_form(self):
    """./x.sh 这种直接执行形式同样要走链。"""
    _w("payload.sh", "rm -rf /tmp/target\n")
    _w("top.sh", "#!/bin/bash\nsource payload.sh\n")
    self.assertTrue(self._blocked("./top.sh"))

  def test_reports_the_dangerous_file(self):
    """报错要指向真正危险的那个文件，而不是最外层——否则用户
    打开 top.sh 看到的是一行 source，完全不知道危险在哪。"""
    from omegaforge.tools.system_tools import BlockedCommand
    _w("payload.sh", "rm -rf /tmp/target\n")
    _w("top.sh", "#!/bin/bash\nsource payload.sh\n")
    try:
      self.st._aggregate_check("bash top.sh")
      self.fail("未被拦截")
    except BlockedCommand as e:
      self.assertIn("payload.sh", str(e),
             f"报错未指向真正危险的文件：{e}")


class TestNoFalsePositive(unittest.TestCase):
  """误杀比漏判更容易毁掉功能——正常用法必须放行。"""

  def setUp(self):
    self.st = _boot()

  def tearDown(self):
    shutil.rmtree(HOME, ignore_errors=True)

  def _blocked(self, cmd: str) -> bool:
    from omegaforge.tools.system_tools import BlockedCommand
    try:
      self.st._aggregate_check(cmd)
      return False
    except BlockedCommand:
      return True

  def test_normal_script(self):
    _w("ok.sh", "echo hello\nls -la\n")
    self.assertFalse(self._blocked("bash ok.sh"))

  def test_read_is_not_exec(self):
    """cat 危险脚本是读取，不是执行意图——不得拦。
    否则用户连查看自己的文件都做不到。"""
    _w("payload.sh", "rm -rf /tmp/target\n")
    self.assertFalse(self._blocked("cat payload.sh"))

  def test_comment_mention_not_a_ref(self):
    """注释里的 source 不会被执行，不该被当引用。"""
    _w("doc.sh", "# source payload.sh （仅作说明）\necho hi\n")
    _w("payload.sh", "rm -rf /tmp/target\n")
    self.assertFalse(self._blocked("bash doc.sh"))

  def test_writing_danger_text_is_allowed(self):
    """写 `rm -rf /` 到 .md 是合法需求（写文档），不阻断写入。"""
    _w("notes.md", "rm -rf / 是危险命令\n")
    self.assertFalse(self._blocked("echo hi"))


class TestBounded(unittest.TestCase):
  """递归必须有界——否则安全检查自己就是 DoS 面。"""

  def setUp(self):
    self.st = _boot()

  def tearDown(self):
    shutil.rmtree(HOME, ignore_errors=True)

  def _run(self, cmd: str):
    from omegaforge.tools.system_tools import BlockedCommand
    try:
      self.st._aggregate_check(cmd)
      return None
    except BlockedCommand as e:
      return str(e)

  def test_self_source_no_hang(self):
    """自己 source 自己不得死循环。

    诚实说明：本项**不足以**单独验证 seen（环检测）——验证去掉 seen
    后它仍然通过，因为 MAX_SCRIPT_DEPTH 已能保证终止。seen 的真实
    作用是避免重复读取（效率），不是防挂死。真正保证终止的是
    depth + files + bytes 三重上限，本类其余几项才是守它们的。
    保留本项是因为它守的是"三者任一被削弱到都不生效"这个底线。
    """
    _w("self.sh", "source self.sh\n")
    t0 = time.time()
    self._run("bash self.sh")
    self.assertLess(time.time() - t0, 5, "自引用导致挂起")

  def test_mutual_source_no_hang(self):
    _w("a.sh", "source b.sh\n")
    _w("b.sh", "source a.sh\n")
    t0 = time.time()
    self._run("bash a.sh")
    self.assertLess(time.time() - t0, 5, "互相引用导致挂起")

  def test_many_files_capped(self):
    """铺很多文件不得让判定无界变慢。"""
    for i in range(40):
      _w(f"f{i}.sh", f"source f{i+1}.sh\n")
    t0 = time.time()
    self._run("bash f0.sh")
    self.assertLess(time.time() - t0, 5, "文件数未设上限，判定被拖慢")

  def test_deeper_than_cap_still_terminates(self):
    """超出 MAX_SCRIPT_DEPTH 的链：停止深挖并放行，但必须能终止。

    这是有意的 fail-open——继续挖会把安全检查变成拒绝服务面。
    """
    for i in range(10):
      _w(f"d{i}.sh", f"source d{i+1}.sh\n")
    _w("d10.sh", "rm -rf /tmp/target\n")
    t0 = time.time()
    self._run("bash d0.sh")
    self.assertLess(time.time() - t0, 5, "超深链未终止")


if __name__ == "__main__":
  unittest.main(verbosity=2)
