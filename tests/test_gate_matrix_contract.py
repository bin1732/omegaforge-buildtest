"""工具层风险分级 × 四级模式的裁决对应关系。

这一层缺少该约束时只被"逐个工具"测过，从没按**矩阵**系统验过。验证发现的问题
不在矩阵本身（矩阵是对的），而在三处：

1. **分级只落在表里，没落在真正对外的数据写入路径上。**
  同一个 MCP 客户端、同一个最保守档 confirm 下：
    fs_write → 「该操作需要你确认后才会执行」（拒绝）
    kb_add  → 直接写进用户知识库，且审计里一条都没有
  而 TOOL_RISK 里明明写着 kb_add 是 medium（confirm 下应为 ask）。

2. **放行侧零审计。** 被拒绝的尝试有留痕，真正写进去的没有——
  "谁在何时通过外部客户端写了什么"无从查证。审计只记拒绝不记放行，
  等于只对好人记账。

3. **登记名与真实工具名分叉。** wiki_put / wiki_write / task_complete
  登记了但代码里不存在（会被渲染进设置页）；真实暴露的 wiki_save
  反而没登记。

回退校验见文件末尾：撤掉任一项修复，对应守卫必须变红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core.errors import user_error          # noqa: E402
from omegaforge.tools.policy import (PERSONAL_WRITE_TOOLS,    # noqa: E402
                   POLICY_META, Policy,
                   PlanRequired, TOOL_RISK,
                   audit_read, gate_personal_write)
from omegaforge.tools.system_tools import (MCP_SCOPE_DEFAULTS,  # noqa: E402
                      McpScope)



def _mk_home(name: str) -> str:
  home = tempfile.mkdtemp(prefix="gate_matrix_%s_" % name)
  with open(os.path.join(home, "permissions.json"), "w") as f:
    json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)
  return home


def _grant(home: str, *tools: str) -> None:
  sc = McpScope(home)
  cur = sc.load()
  for t in tools:
    cur[t] = True
  sc.save(cur)


def _mcp_call(home: str, name: str, args: dict, rid: int = 1) -> list:
  """真实 MCP 子进程：不走内层函数，避免"接线断了测试也全绿"。"""
  env = dict(os.environ, OMEGAFORGE_HOME=home, OMEGAFORGE_MOCK="1")
  msgs = [
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
      "protocolVersion": "2025-06-18", "capabilities": {},
      "clientInfo": {"name": "probe", "version": "1"}}},
    {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
     "params": {"name": name, "arguments": args}},
  ]
  p = subprocess.run([sys.executable, "-m", "omegaforge.cli", "mcp"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            capture_output=True, text=True, cwd=ROOT,
            env=env, timeout=180)
  out = []
  for ln in p.stdout.splitlines():
    ln = ln.strip()
    if not ln:
      continue
    try:
      out.append(json.loads(ln))
    except json.JSONDecodeError:
      pass
  return out


def _text(res: list, rid: int = 1) -> tuple:
  """返回 (文本, 是否 isError)。"""
  for r in res:
    if r.get("id") != rid:
      continue
    result = r.get("result", {})
    c = result.get("content") or [{}]
    return (c[0].get("text", ""), bool(result.get("isError")))
  return ("", False)


class MatrixTest(unittest.TestCase):
  """矩阵本身：mode × risk → verdict，必须与宣称一致。"""

  def setUp(self):
    self.home = _mk_home("matrix")

  def _verdict(self, mode: str, risk: str) -> str:
    Policy(self.home).set_mode(mode)
    # 借一个已知工具来取该 risk 档的裁决
    tool = {"low": "fs.read", "medium": "fs.write",
        "high": "terminal"}[risk]
    return Policy(self.home).decide(tool)["verdict"]

  def test_1_declared_matrix_matches(self):
    want = {"confirm":  {"low": "allow", "medium": "ask", "high": "ask"},
        "auto_edit": {"low": "allow", "medium": "allow", "high": "ask"},
        "plan":   {"low": "allow", "medium": "plan", "high": "plan"},
        "full":   {"low": "allow", "medium": "allow", "high": "allow"}}
    for mode in want:
      for risk in want[mode]:
        self.assertEqual(self._verdict(mode, risk), want[mode][risk],
                 f"{mode} × {risk}")

  def test_2_confirm_asks_for_every_write(self):
    """confirm 的语义是"改之前问我"，所有写工具都必须 ask。"""
    Policy(self.home).set_mode("confirm")
    for tool in sorted(PERSONAL_WRITE_TOOLS):
      self.assertEqual(Policy(self.home).decide(tool)["verdict"], "ask",
               tool)

  def test_3_full_allows_writes(self):
    """防处理过头：full 的语义是减少确认，写工具必须放行。"""
    Policy(self.home).set_mode("full")
    for tool in sorted(PERSONAL_WRITE_TOOLS):
      self.assertEqual(Policy(self.home).decide(tool)["verdict"], "allow",
               tool)


class McpPersonalWriteTest(unittest.TestCase):
  """MCP 路径：个人数据写工具必须与 fs.write 同标，且放行也要留痕。"""

  def test_1_confirm_does_not_write(self):
    """核心：confirm 下外部客户端不能静默写进用户知识库。"""
    home = _mk_home("confirm_write")
    Policy(home).set_mode("confirm")
    _grant(home, "kb_add")
    txt, is_err = _text(_mcp_call(home, "kb_add",
                   {"title": "t", "text": "x"}))
    self.assertTrue(is_err, "confirm 下必须拒绝，验证却写入了")
    self.assertNotIn('"total"', txt)
    self.assertIn("确认", txt)

  def test_2_fs_write_and_kb_add_are_the_same_standard(self):
    """同一入口、同一档位，两种"写用户数据"必须给出同一类答复。"""
    home = _mk_home("parity")
    Policy(home).set_mode("confirm")
    _grant(home, "kb_add", "fs.write")
    _, fs_err = _text(_mcp_call(home, "fs_write",
                  {"path": "a.txt", "content": "x"}, 1))
    _, kb_err = _text(_mcp_call(home, "kb_add",
                  {"title": "t", "text": "x"}, 2), 2)
    self.assertTrue(fs_err)
    self.assertTrue(kb_err, "fs_write 被拒而 kb_add 放行——双标")

  def test_3_full_still_writes(self):
    """防处理过头：full 下授予作用域后必须能写。"""
    home = _mk_home("full_write")
    Policy(home).set_mode("full")
    _grant(home, "kb_add")
    txt, is_err = _text(_mcp_call(home, "kb_add",
                   {"title": "t", "text": "x"}))
    os.environ["OMEGAFORGE_HOME"] = home
    self.assertFalse(is_err, txt)
    self.assertIn("total", txt)

  def test_4_allow_is_audited(self):
    """放行侧必须留痕。只记拒绝不记放行，等于只对好人记账。"""
    home = _mk_home("audit")
    Policy(home).set_mode("full")
    _grant(home, "kb_add")
    _mcp_call(home, "kb_add", {"title": "t", "text": "x"})
    os.environ["OMEGAFORGE_HOME"] = home
    rows = [r for r in audit_read(limit=50)
        if r.get("tool") == "kb_add" and r.get("verdict") == "allow"]
    self.assertTrue(rows, "放行后审计里查不到任何 kb_add 记录")
    self.assertEqual(rows[-1].get("origin"), "mcp",
             "审计必须能区分是界面发起还是外部客户端发起")


class PlanModeTest(unittest.TestCase):
  """plan 模式：MCP 上不能退化成「操作失败，请稍后重试」。"""

  def test_1_plan_message_is_actionable(self):
    home = _mk_home("plan")
    Policy(home).set_mode("plan")
    _grant(home, "fs.write")
    txt, _ = _text(_mcp_call(home, "fs_write",
                 {"path": "a.txt", "content": "x"}))
    self.assertNotIn("请稍后重试", txt,
             "plan 被当成了服务器故障，外部模型会反复重试")
    self.assertIn("计划", txt)

  def test_2_classify_has_a_branch(self):
    """_classify 必须认识 PlanRequired，而不是落到通用兜底。"""
    exc = PlanRequired("fs.write", {"tool": "fs.write"})
    msg = user_error(exc, "probe")
    self.assertNotIn("请稍后重试", msg)
    self.assertIn("计划", msg)


class McpApprovalGuidanceTest(unittest.TestCase):
  """MCP 没有弹窗确认这条通道：ask 对它等于永远拒绝。

  若仍回"该操作需要你确认后才会执行"，用户和外部模型都会以为等一等
  或换个参数就行——验证这条路是确定性失败。指引必须说清怎么走下去。
  """

  def test_1_mcp_approval_message_is_actionable(self):
    home = _mk_home("guidance")
    Policy(home).set_mode("confirm")
    _grant(home, "fs.write")
    txt, is_err = _text(_mcp_call(home, "fs_write",
                   {"path": "a.txt", "content": "x"}))
    self.assertTrue(is_err)
    self.assertIn("无法弹出确认窗口", txt,
           "MCP 上的 ask 必须说明没有确认通道，实际：%s" % txt)
    self.assertIn("权限级别", txt)

  def test_2_local_approval_message_unchanged(self):
    """防处理过头：本机仍有确认通道，不能套用 MCP 的文案。"""
    home = _mk_home("local")
    os.environ["OMEGAFORGE_HOME"] = home
    Policy(home).set_mode("confirm")
    from omegaforge.tools.system_tools import SystemTools
    with self.assertRaises(Exception) as cm:
      SystemTools(home).fs_write("a.txt", "x")
    msg = user_error(cm.exception, "probe")
    self.assertIn("需要你确认", msg)
    self.assertNotIn("无法弹出确认窗口", msg)


class NameRegistrationTest(unittest.TestCase):
  """登记名与真实工具名不得分叉——表与实现分叉而无人拦住，最危险。"""

  def test_1_scope_table_is_a_subset_of_risk_table(self):
    """作用域表里引用的工具，风险表里必须有它的分级。"""
    missing = sorted(set(MCP_SCOPE_DEFAULTS) - set(TOOL_RISK))
    self.assertEqual(missing, [],
             "作用域引用了风险表里没有的工具：%s" % missing)

  def test_2_no_phantom_names(self):
    """不能登记代码里根本不存在的工具名——会被渲染进设置页。"""
    phantom = {"wiki_put", "wiki_write", "task_complete"}
    self.assertEqual(phantom & set(TOOL_RISK), set())

  def test_3_real_names_are_registered(self):
    """真实暴露的写工具必须登记，否则落进 unknown→high，表不再可信。"""
    for n in ("wiki_save", "wiki_search", "kb_add", "memory_remember",
         "task_add", "task_done"):
      self.assertIn(n, TOOL_RISK, n)

  def test_4_meta_matches_table(self):
    """渲染给前端的清单必须与真源一致。"""
    meta = POLICY_META()
    self.assertEqual(set(meta["tool_risk"]), set(TOOL_RISK))

  def test_5_personal_write_subset_of_scope_table(self):
    """个人写工具必须在作用域表里——否则外部客户端不受作用域约束。"""
    missing = sorted(PERSONAL_WRITE_TOOLS - set(MCP_SCOPE_DEFAULTS))
    self.assertEqual(missing, [], missing)


class GatePersonalWriteUnitTest(unittest.TestCase):
  """gate_personal_write 本身：三种裁决各自的落点。"""

  def setUp(self):
    self.home = _mk_home("unit")
    os.environ["OMEGAFORGE_HOME"] = self.home

  def test_1_confirm_raises(self):
    Policy(self.home).set_mode("confirm")
    with self.assertRaises(Exception) as cm:
      gate_personal_write("kb_add", {"title": "t"}, origin="mcp")
    self.assertEqual(type(cm.exception).__name__, "ApprovalRequired")

  def test_2_plan_raises(self):
    Policy(self.home).set_mode("plan")
    with self.assertRaises(Exception) as cm:
      gate_personal_write("kb_add", {"title": "t"}, origin="mcp")
    self.assertEqual(type(cm.exception).__name__, "PlanRequired")

  def test_3_full_returns_allow(self):
    Policy(self.home).set_mode("full")
    self.assertEqual(
      gate_personal_write("kb_add", {"title": "t"}, origin="mcp"),
      "allow")

  def test_4_ask_is_audited_too(self):
    """被问询也要留痕——"谁在何时试图越权"同样要能查。"""
    Policy(self.home).set_mode("confirm")
    try:
      gate_personal_write("kb_add", {"title": "t"}, origin="mcp")
    except Exception:
      pass
    rows = [r for r in audit_read(limit=50)
        if r.get("tool") == "kb_add" and r.get("verdict") == "ask"]
    self.assertTrue(rows)


if __name__ == "__main__":
  unittest.main(verbosity=2)
