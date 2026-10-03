"""MCP 作用域：外部客户端不得继承本机执行权限。

起因（验证 2026-09-17）：用户为本机使用开启全部系统能力 + full（完全访问）
模式后，一个刚挂载、未被授权的第三方 MCP host 立刻能用 fs.list 列出
home 目录，拿到 permissions.json / context_ledger.json 等内部文件名。

根因：能力开关是进程级的，没有"调用来源"概念。但"用户信任自己在
本 app 里点的操作"不等于"用户信任另一个应用里的远程模型"。

更硬的约束在审批：ApprovalRequired 是一条**人机交互通道**，假设界面能
弹窗、用户能点确认。MCP stdio 下 host 是另一个进程，这条通道不存在，
"需要确认才执行"的裁决会退化成永远无法完成的悬空状态——必须显式收口。
"""
from __future__ import annotations

import json
import os
import shutil
import unittest
from unittest import mock

from omegaforge.mcp_server import McpCore, _SYS_DISPATCH, _sys_available
from omegaforge.tools.system_tools import McpScope, tool_dispatch


def _write_home(home: str, mode: str = "full", perms: dict | None = None,
        scope: dict | None = None) -> None:
  os.makedirs(home, exist_ok=True)
  with open(os.path.join(home, "policy.json"), "w") as f:
    json.dump({"mode": mode}, f)
  if perms is not None:
    with open(os.path.join(home, "permissions.json"), "w") as f:
      json.dump(perms, f)
  if scope is not None:
    with open(os.path.join(home, "mcp_scope.json"), "w") as f:
      json.dump(scope, f)


class TestOriginNotInherited(unittest.TestCase):
  """本机开了的能力，MCP 客户端不能自动获得。"""

  def setUp(self):
    self.home = os.path.abspath(".omegaforge_test_mcp_scope")
    shutil.rmtree(self.home, ignore_errors=True)
    _write_home(self.home, mode="full",
          perms={"terminal": True, "fs": True, "web_fetch": True})
    self.env = mock.patch.dict(os.environ, {"OMEGAFORGE_HOME": self.home})
    self.env.start()

  def tearDown(self):
    self.env.stop()

  def test_exec_and_write_denied_despite_local_full(self):
    """本机 full + 全部能力开启，MCP 仍不得执行/写入/联网。"""
    for name, args in (("run_command", {"cmd": "whoami"}),
              ("fs_write", {"path": "x.txt", "content": "a"}),
              ("web_fetch", {"url": "http://example.com"})):
      with self.assertRaises(Exception, msg=f"{name} 不应被放行"):
        tool_dispatch(name, args, origin="mcp")

  def test_readonly_still_allowed(self):
    """只读类默认可用——MCP 客户端通常就是为了读上下文，不能误杀。"""
    r = tool_dispatch("fs_list", {"path": "."}, origin="mcp")
    self.assertIn("items", r, "只读目录被误杀，MCP 基本用途受损")

  def test_local_call_unaffected(self):
    """同一环境，本机来源不受作用域限制（作用域只管外部）。"""
    try:
      tool_dispatch("fs_list", {"path": "."})
    except Exception as e:
      self.fail(f"本机调用被 MCP 作用域误伤：{e}")

  def test_scope_open_then_allowed(self):
    """显式开启作用域后可用——收口不是封死。"""
    _write_home(self.home, scope={"terminal": True, "fs.write": True,
                   "web_fetch": True})
    self.assertTrue(McpScope().load()["terminal"])
    # 开启后门禁不再因作用域拒绝（此处只验证开关生效，不实际执行命令）
    self.assertTrue(_sys_available("run_command"))


class TestMcpScopeDeniedMessage(unittest.TestCase):
  """拒绝文案必须指向 MCP 作用域，而不是诱导去开本机能力。"""

  def test_message_points_to_mcp_scope(self):
    from omegaforge.core.errors import user_error
    from omegaforge.tools.system_tools import McpScopeDenied
    msg = user_error(McpScopeDenied("terminal"))
    self.assertIn("MCP", msg)
    self.assertIn("不继承", msg)
    # 关键：不能引导调用方去开"系统能力"——那是本机开关，
    # 一开就对所有挂载的 host 生效，正是这里要防的传导。
    self.assertNotIn("系统能力", msg)


class TestNoGhostTools(unittest.TestCase):
  """tools/list 不得列出永远调不通的工具。"""

  def setUp(self):
    self.home = os.path.abspath(".omegaforge_test_ghost")
    shutil.rmtree(self.home, ignore_errors=True)
    _write_home(self.home, mode="full",
          perms={"terminal": True, "fs": True, "web_fetch": True})
    self.env = mock.patch.dict(os.environ, {"OMEGAFORGE_HOME": self.home})
    self.env.start()
    self.core = McpCore()

  def tearDown(self):
    self.env.stop()

  def _listed(self):
    resp = self.core.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return [t["name"] for t in resp["result"]["tools"]]

  def test_unavailable_sys_tools_not_listed(self):
    listed = self._listed()
    for name in ("run_command", "fs_write", "web_fetch"):
      self.assertNotIn(name, listed,
               f"{name} 作用域未开却对外列出——调用必失败，"
               f"且会诱导模型去开它打不开的本机设置")

  def test_non_sys_tools_always_listed(self):
    """知识库/记忆/wiki/任务等不受能力开关管，不应被误过滤。"""
    listed = self._listed()
    for name in ("kb_search", "memory_recall", "wiki_get", "task_list"):
      self.assertIn(name, listed, f"{name} 被误过滤")

  def test_listed_implies_callable(self):
    """列出的系统工具必须真的能过门禁——承诺与能力必须一致。"""
    for name in self._listed():
      if name in _SYS_DISPATCH:
        self.assertTrue(_sys_available(name),
                f"{name} 列出了但当前不可用（死工具）")


class TestOriginAudited(unittest.TestCase):
  """审计必须能区分"本机操作"与"外部 agent 发起"。"""

  def setUp(self):
    self.home = os.path.abspath(".omegaforge_test_origin")
    shutil.rmtree(self.home, ignore_errors=True)
    _write_home(self.home, mode="full",
          perms={"terminal": True, "fs": True, "web_fetch": True})
    self.env = mock.patch.dict(os.environ, {"OMEGAFORGE_HOME": self.home})
    self.env.start()

  def tearDown(self):
    self.env.stop()

  def test_origin_recorded(self):
    try:
      tool_dispatch("run_command", {"cmd": "whoami"}, origin="mcp")
    except Exception:
      pass
    found = []
    for fn in ("audit.jsonl", "tools_audit.jsonl"):
      fp = os.path.join(self.home, fn)
      if not os.path.isfile(fp):
        continue
      for line in open(fp, encoding="utf-8"):
        try:
          d = json.loads(line)
        except Exception:
          continue
        if d.get("reason") == "mcp_scope_off":
          found.append(d)
    self.assertTrue(found, "拒绝事件未写入审计")
    self.assertTrue(all(d.get("origin") == "mcp" for d in found),
            "审计里看不出这次是外部 MCP 客户端发起的")

  def test_origin_reaches_audit_via_protocol(self):
    """必须走 handle() 验证接线，不能只测 tool_dispatch(origin=...)。

    只测内层的话，mcp_server 里哪怕漏传 origin="mcp"，测试照样全绿——
    origin 就成了永远不生效的死参数。回退校验抓到过一次（第 4 项
    "撤掉来源透传" 未抓到），所以这条必须走协议层。
    """
    McpCore().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "fs_list", "arguments": {"path": "."}}})
    hit = []
    for fn in ("audit.jsonl", "tools_audit.jsonl"):
      fp = os.path.join(self.home, fn)
      if not os.path.isfile(fp):
        continue
      for line in open(fp, encoding="utf-8"):
        try:
          d = json.loads(line)
        except Exception:
          continue
        if d.get("tool") in ("fs.list", "fs_list"):
          hit.append(d)
    self.assertTrue(hit, "协议层调用未写入审计")
    self.assertTrue(any(d.get("origin") == "mcp" for d in hit),
            "MCP 层未透传 origin，审计无法区分来源")


if __name__ == "__main__":
  unittest.main()
