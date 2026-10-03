"""MCP 协议合规与健壮性守卫。

本文件里的每一条都对应一个可复现过的问题，不是理论推演：

 1. 客户端发 "[]" / "null" / "123" 这类合法 JSON 但不是对象的消息，
   会让整个 MCP 服务进程崩溃退出（rc=1），此后所有请求无响应。
   根因：handle() 里 msg.get 写在 try 之外，AttributeError 冒泡出主循环。
 2. 工具执行失败缺少该约束时一律返回 JSON-RPC error（-32000）。那是协议级失败，
   客户端会当成连接问题；MCP 规定工具失败应返回 result + isError，
   这样模型才看得到原因并自我纠正（换参数 / 换工具）。
 3. schema 已声明 required，但 call_tool 用 args["x"] 硬取，缺参数即
   KeyError → 被脱敏层归为内部错误，调用方只看到"操作失败，请稍后重试"。

进程级崩溃必须走真实子进程验证——在进程内 try/except 是抓不到
"主循环挂掉"这件事的。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "omegaforge")

if ROOT not in sys.path:
  sys.path.insert(0, ROOT)
from omegaforge.mcp_server import McpCore # noqa: E402


def _spawn(messages: list[str]) -> tuple[int, list[dict], str]:
  """起一个真实 MCP 服务进程，喂入消息，返回 (退出码, 响应列表, stderr)。"""
  env = dict(os.environ)
  env.pop("OMEGAFORGE_PROVIDER", None)
  p = subprocess.Popen(
    [sys.executable, "-m", "omegaforge.mcp_server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)
  for m in messages:
    try:
      p.stdin.write(m + "\n")
      p.stdin.flush()
    except BrokenPipeError:
      break
  try:
    p.stdin.close()
  except Exception: # noqa: BLE001
    pass
  out = p.stdout.read()
  try:
    p.wait(timeout=20)
  except subprocess.TimeoutExpired:
    p.kill()
  err = p.stderr.read()
  resp = []
  for line in out.splitlines():
    if line.strip():
      try:
        resp.append(json.loads(line))
      except json.JSONDecodeError:
        resp.append({"_raw": line})
  return p.returncode, resp, err


class TestMcpRobustness(unittest.TestCase):
  """一条畸形消息不得打死整个服务。"""

  def test_non_dict_json_does_not_kill_server(self):
    """[] / null / "str" / 123 曾经让进程 rc=1 退出。"""
    for raw in ["[]", "null", '"str"', "123", "true"]:
      with self.subTest(msg=raw):
        rc, _, _ = _spawn([raw])
        self.assertEqual(0, rc,
                 f"{raw} 让服务进程崩溃退出（rc={rc}），"
                 f"此后客户端所有工具调用都会无响应")

  def test_non_dict_json_returns_invalid_request(self):
    """非对象消息应回 -32600，而不是让进程崩。"""
    for raw in ["null", '"str"', "123", "true"]:
      with self.subTest(msg=raw):
        _, resp, _ = _spawn([raw])
        self.assertEqual(1, len(resp), f"{raw} 应有且仅有一个响应")
        self.assertEqual(-32600, resp[0]["error"]["code"])

  def test_server_still_alive_after_malformed_message(self):
    """畸形消息之后服务必须还能正常应答——这是"没崩"的真正含义。"""
    ping = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    for raw in ["[]", "null", "123", "not json at all"]:
      with self.subTest(msg=raw):
        _, resp, _ = _spawn([raw, ping])
        last = resp[-1]
        self.assertEqual(9, last.get("id"),
                 f"{raw} 之后服务未能正常应答 ping")
        self.assertIn("result", last)

  def test_missing_method_is_invalid_request(self):
    """缺 method 必须回 -32600。

    缺失时若被当成未知方法（-32601）或落到内部错误（-32000），客户端按
    协议做的分类处理会走错分支：前者被当成"方法名拼错"去重试，后者被当成
    连接故障去断开重连——而真正的原因只是这条消息没带 method。
    """
    for msg in [{"jsonrpc": "2.0", "id": 3},
                {"jsonrpc": "2.0", "id": 3, "method": ""},
                {"jsonrpc": "2.0", "id": 3, "method": 7}]:
      with self.subTest(msg=msg):
        _, resp, _ = _spawn([json.dumps(msg)])
        self.assertEqual(1, len(resp))
        self.assertEqual(-32600, resp[0]["error"]["code"],
                     f"{msg} 未回无效请求，客户端会按错的类型处理")

  def test_empty_batch_has_no_response(self):
    """空 batch 数组按 JSON-RPC 规范不产生响应。"""
    _, resp, _ = _spawn(["[]"])
    self.assertEqual([], resp)


class TestMcpToolErrorContract(unittest.TestCase):
  """工具失败必须走 isError，让调用方看得到原因。"""

  def setUp(self):
    sys.path.insert(0, ROOT)
    from omegaforge.mcp_server import McpCore
    self.core = McpCore()

  @staticmethod
  def _grant_personal_write(mode="full"):
    """授予"写个人数据"的 MCP 作用域，并把权限级别调到可写档。

    个人数据的写工具（task_add / kb_add / memory_remember / wiki_save…）
    默认关闭：外部 MCP 客户端不继承本机权限。要测"正常调用不被误杀"，
    必须先显式授权——这正是作用域在起作用，不是误伤。

    权限级别同样要调开：作用域回答"允不允许外部客户端做这类事"，
    四级矩阵回答"这台机器当前的自主程度"，两者是相乘关系。默认档
    confirm 下连本机的 fs.write 都要先确认，MCP 更不可能例外——
    本类用例测的是**入参校验**不误杀，不是政策放行，所以把政策
    变量调到"允许写"再验校验本身。
    """
    from unittest import mock
    import omegaforge.mcp_server as ms
    from omegaforge.tools.policy import Policy
    scope = {k: True for k in ms.MCP_SCOPE_DEFAULTS}
    return (mock.patch.object(ms.McpScope, "load", lambda self: scope),
        mock.patch.object(Policy, "mode", lambda self: mode))

  def _call(self, name, arguments, mid=1):
    return self.core.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}})

  def test_tool_error_is_iserror_not_jsonrpc_error(self):
    """未知工具 → result.isError，不是 error.code。"""
    r = self._call("no_such_tool", {})
    self.assertNotIn("error", r, "工具失败不该升级为协议级错误")
    self.assertTrue(r["result"].get("isError"), "必须带 isError=true")

  def test_tool_error_text_is_actionable(self):
    """错误信息必须说清是哪个工具、缺哪个参数，否则无法自我纠正。"""
    r = self._call("no_such_tool", {})
    self.assertIn("no_such_tool", r["result"]["content"][0]["text"])

    r = self._call("kb_add", {})
    text = r["result"]["content"][0]["text"]
    self.assertTrue(r["result"].get("isError"))
    # 点名用面向调用方的中文说法，不能用内部字段名
    self.assertIn("标题", text)
    self.assertIn("正文", text)
    self.assertNotIn("title", text)

  def test_missing_required_params_named_explicitly(self):
    """缺必填参数要逐一点名，不能只说"请求内容有误"。"""
    r = self._call("wiki_save", {"slug": "s"})
    text = r["result"]["content"][0]["text"]
    self.assertTrue(r["result"].get("isError"))
    self.assertIn("还缺少必填内容", text, f"应说明缺了必填项，实际：{text}")
    self.assertIn("标题", text, f"应点名缺失的项，实际：{text}")

  def test_wrong_type_reported_in_chinese(self):
    """类型错误要说清是哪个参数、应该是什么类型。"""
    r = self._call("task_add", {"text": "x", "priority": "abc"})
    text = r["result"]["content"][0]["text"]
    self.assertTrue(r["result"].get("isError"))
    # 点名用中文说法；内部字段名同样不能出现
    self.assertIn("优先级", text)
    self.assertNotIn("priority", text)
    for bad in ["integer", "str", "TypeError"]:
      self.assertNotIn(bad, text, f"不应把内部类型名/异常名甩给调用方：{text}")

  def test_blocked_command_keeps_its_specific_reason(self):
    """安全拦截有多种原因，不能被压成一句"该命令具有破坏性"。

    验证：访问 169.254.169.254（云元数据）被拒时，用户看到的是
    "该命令具有破坏性，已被安全策略拦截"——把"网址不允许"说成了
    "命令危险"，调用方会以为换条命令就行，实际换任何命令都一样。
    """
    r = self._call("web_fetch", {"url": "http://169.254.169.254/latest/meta-data/"})
    self.assertTrue(r["result"].get("isError"))
    text = r["result"]["content"][0]["text"]
    self.assertIn("169.254.169.254", text,
           f"拦截文案丢失了具体原因，实际：{text}")
    self.assertNotIn("破坏性", text,
             f"内网地址被拒不该说成命令具有破坏性：{text}")

  def test_normal_call_still_works(self):
    """正常调用不能被校验误杀。"""
    p_scope, p_mode = self._grant_personal_write()
    with p_scope, p_mode:
      r = self._call("task_add", {"text": "MCP 合规守卫用例", "priority": 2})
    self.assertIn("result", r)
    self.assertFalse(r["result"].get("isError"))
    self.assertIn("MCP 合规守卫用例", r["result"]["content"][0]["text"])

  def test_optional_params_may_be_omitted(self):
    """非必填参数不传必须放行——校验过严会误杀正常调用。"""
    p_scope, p_mode = self._grant_personal_write()
    with p_scope, p_mode:
      r = self._call("task_add", {"text": "只填必填项"})
    self.assertFalse(r["result"].get("isError"),
             f"可选参数未传却被拒绝：{r}")

  def test_scope_denies_even_when_policy_allows(self):
    """作用域这道必须独立成立，不能只靠权限矩阵兜住。

    写个人数据的工具面前有两道：作用域（`MCP_SCOPE_DEFAULTS`）与四级权限
    矩阵（`gate_personal_write`）。若只验默认档，两道同时拦，撤掉作用域
    那道用例依然全绿——作用域是否真的拦，无法从默认档的用例里看出来，
    而它是外部客户端与本机权限之间唯一的开关。

    因此本用例把权限档调到 full（矩阵放行），只留作用域：此时仍必须拒绝。
    """
    from unittest import mock
    import omegaforge.mcp_server as ms
    from omegaforge.tools.policy import Policy
    with mock.patch.object(Policy, "mode", lambda self: "full"):
      for name, args in [("task_add", {"text": "不该写入"}),
                         ("kb_add", {"title": "t", "text": "x"}),
                         ("memory_remember", {"fact": "f"}),
                         ("wiki_save", {"slug": "s", "title": "t"})]:
        with self.subTest(tool=name):
          r = self.core.handle({"jsonrpc": "2.0", "id": 1,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
          self.assertTrue(r.get("result", {}).get("isError"),
                          f"权限档放行后 {name} 也未被作用域拦住：{r}")

  def test_personal_write_denied_by_default(self):
    """写个人数据的工具默认关闭：外部客户端不继承本机权限。

    缺少该约束时：默认最保守档（confirm）下，外部 MCP 客户端照样
    能往用户知识库/记忆/Wiki/待办里写，且审计里一条都没有——而我们
    对外标注还写着 destructiveHint=True。
    """
    for name, args in [("task_add", {"text": "不该写入"}),
              ("kb_add", {"title": "t", "text": "x"}),
              ("memory_remember", {"fact": "f"}),
              ("wiki_save", {"slug": "s", "title": "t"})]:
      with self.subTest(tool=name):
        r = self._call(name, args)
        self.assertTrue(r["result"].get("isError"),
                f"{name} 默认可被外部客户端写入：{r}")
    # 读工具不受影响：接 MCP 的主要理由就是"让外部 agent 用我的知识库"
    for name, args in [("kb_search", {"query": "x"}),
              ("memory_recall", {"query": "x"})]:
      with self.subTest(tool=name):
        r = self._call(name, args)
        self.assertFalse(r["result"].get("isError"),
                 f"读工具被作用域误伤：{r}")


class TestMcpProtocolShape(unittest.TestCase):
  """协议层形状：版本协商、错误码、batch。"""

  def setUp(self):
    sys.path.insert(0, ROOT)
    from omegaforge.mcp_server import McpCore
    self.core = McpCore()

  def test_protocol_version_negotiation(self):
    """客户端要的版本若已实现就用它，否则回落到我们实现的版本。"""
    r = self.core.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18",
                     "capabilities": {},
                     "clientInfo": {"name": "t", "version": "1"}}})
    self.assertEqual("2025-06-18", r["result"]["protocolVersion"])

    r = self.core.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
               "params": {"protocolVersion": "2099-01-01",
                     "capabilities": {},
                     "clientInfo": {"name": "t", "version": "1"}}})
    self.assertEqual("2024-11-05", r["result"]["protocolVersion"])

  def test_unknown_method_uses_standard_code(self):
    r = self.core.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    self.assertEqual(-32601, r["error"]["code"])

  def test_bad_params_use_standard_code(self):
    r = self.core.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call"})
    self.assertEqual(-32602, r["error"]["code"])
    r = self.core.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": 123}})
    self.assertEqual(-32602, r["error"]["code"])

  def test_notification_returns_none(self):
    r = self.core.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    self.assertIsNone(r, "通知不得产生响应，否则会污染请求/响应配对")

  def test_batch_processes_each_item(self):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        "not-an-object"]
    out = self.core.handle_batch(msgs)
    self.assertIsInstance(out, list)
    self.assertEqual(3, len(out), "batch 中每条都要有对应响应")

  def test_tools_list_schema_matches_validator(self):
    """schema 声明的 required 必须被 _validate 真正校验，两边不能走偏。"""
    for tool in self.core.TOOLS:
      name = tool["name"]
      required = (tool.get("inputSchema") or {}).get("required") or []
      if not required:
        continue
      with self.subTest(tool=name):
        try:
          self.core._validate(name, {})
        except Exception as e: # noqa: BLE001
          msg = str(e)
          # 点名的是面向调用方的说法，不是内部字段名。
          # 断言钉死字段名的话，文案一旦改成中文就会误报——
          # 那时报错指向的是测试，不是产品。
          for key in required:
            self.assertNotIn(
              key, msg,
              f"{name} 把内部字段名甩给了调用方，实际：{msg}")
          named = self._named_items(msg)
          self.assertEqual(
            len(required), len(named),
            f"{name} 声明必填 {required}，文案只点名了 {named}，"
            f"实际：{msg}")
        else:
          self.fail(f"{name} 声明了必填 {required} 且不校验")

  @staticmethod
  def _named_items(msg: str) -> list[str]:
    """从"还缺少必填内容：A、B"里取出被点名的项。"""
    tail = msg.split("：")[-1] if "：" in msg else msg
    return [x.strip() for x in tail.split("、") if x.strip()]


class TestToolAnnotations(unittest.TestCase):
  """工具语义标注：把内部四级风险翻译给外部 host。

  外部 MCP host（Claude Desktop / Cursor）看不到我们进程内的 TOOL_RISK，
  只能靠 annotations 决定"要不要弹确认、弹多大"。缺了这一层，
  run_command 和 kb_search 在用户面前长得一模一样——门禁边界出不了进程。
  """

  def setUp(self):
    self.core = McpCore()

  def test_every_tool_has_annotations(self):
    missing = [t["name"] for t in self.core._public_tools()
          if "annotations" not in t]
    self.assertEqual([], missing, f"以下工具缺语义标注：{missing}")

  def test_annotations_do_not_corrupt_schema(self):
    """标注只能叠加，不能污染 _validate 依赖的 TOOL 原表。"""
    for spec in self.core.TOOLS:
      self.assertNotIn("annotations", spec,
               f"{spec['name']} 的 schema 被标注污染，校验会走偏")

  def test_dangerous_tools_marked_destructive(self):
    for name in ("run_command", "fs_write"):
      ann = self.core.TOOL_ANNOTATIONS[name]
      self.assertTrue(ann["destructiveHint"], f"{name} 未标记破坏性")
      self.assertFalse(ann["readOnlyHint"], f"{name} 误标为只读")

  def test_readonly_tools_not_marked_destructive(self):
    for name in ("fs_read", "fs_list", "kb_search", "wiki_get", "task_list"):
      ann = self.core.TOOL_ANNOTATIONS[name]
      self.assertTrue(ann["readOnlyHint"], f"{name} 应标记只读")
      self.assertFalse(ann["destructiveHint"], f"{name} 误标破坏性")

  def test_open_world_only_for_network_and_exec(self):
    for name in ("web_fetch", "run_command"):
      self.assertTrue(self.core.TOOL_ANNOTATIONS[name]["openWorldHint"],
              f"{name} 触及外部，应标 openWorldHint")
    for name in ("fs_read", "fs_write", "kb_search"):
      self.assertFalse(self.core.TOOL_ANNOTATIONS[name]["openWorldHint"],
               f"{name} 是本域操作，不应标 openWorldHint")

  def test_every_tool_has_chinese_title(self):
    for t in self.core._public_tools():
      title = t["annotations"]["title"]
      self.assertTrue(title and any("\u4e00" <= c <= "\u9fff" for c in title),
              f"{t['name']} 的 title 不是中文：{title!r}")


  def test_annotations_reach_protocol(self):
    """必须走 handle() 验证接线，而不是只调 _public_tools()。

    只测内层方法的话，tools/list 里哪怕退回 self.TOOLS（不带标注），
    测试照样全绿——标注就成了永远不生效的死代码。回退校验抓到过一次。

    作用域全开后再断言：tools/list 按可用性过滤，作用域关闭时
    run_command 本就不该出现，此时取它只会 KeyError——那是过滤在
    生效，不是标注丢了。
    """
    from unittest import mock
    from omegaforge.tools.system_tools import McpScope, Permissions
    allon_scope = {"terminal": True, "fs.write": True, "web_fetch": True}
    allon_cap = {"terminal": True, "fs": True, "web_fetch": True}
    with mock.patch.object(McpScope, "load", lambda self: allon_scope), \
       mock.patch.object(Permissions, "load", lambda self: allon_cap):
      resp = self.core.handle({"jsonrpc": "2.0", "id": 1,
                   "method": "tools/list"})
    tools = resp["result"]["tools"]
    self.assertTrue(tools)
    for t in tools:
      self.assertIn("annotations", t,
             f"tools/list 输出的 {t['name']} 未带标注（接线断了）")
    by_name = {t["name"]: t for t in tools}
    self.assertTrue(by_name["run_command"]["annotations"]["destructiveHint"])
    self.assertTrue(by_name["fs_read"]["annotations"]["readOnlyHint"])


class TestArgAliases(unittest.TestCase):
  """参数别名：MCP 客户端是别人的模型，只能按语义猜参数名。

  工具叫 run_command，模型大概率传 command；我们内部叫 cmd。
  内部前端/CLI 对不上可以改代码对齐，MCP 对不上是永久失效——
  错误文案里给的正是它没传的那个键，模型无从纠正，形成死循环。
  """

  # (工具, 模型可能传的入参, 规范名, 归一化后规范名应拿到的值)
  CASES = [
    ("run_command",   {"command": "echo hi"},     "cmd",     "echo hi"),
    ("fs_write",     {"path": "a.txt", "text": "x"}, "content",   "x"),
    ("task_add",     {"content": "写报告"},      "text",    "写报告"),
    ("task_done",    {"id": "abc"},         "task_id",   "abc"),
    ("kb_add",      {"title": "t", "content": "c"}, "text",    "c"),
    ("memory_remember", {"text": "地球是圆的"},     "fact",    "地球是圆的"),
    ("omegaforge_run",  {"genome": "g", "task": "t"},  "genome_path", "g"),
  ]

  def setUp(self):
    self.core = McpCore()

  def test_alias_maps_to_canonical(self):
    for name, args, canon, value in self.CASES:
      with self.subTest(tool=name):
        out = self.core._normalize_args(name, args)
        self.assertIn(canon, out, f"{name}: {args} 未归一到 {canon}")
        self.assertEqual(value, out[canon],
                 f"{name}: 别名转换时值被改写，得到 {out.get(canon)!r}")

  def test_canonical_wins_over_alias(self):
    """规范名与别名同时出现时以规范名为准，不替模型猜。"""
    out = self.core._normalize_args("run_command",
                    {"cmd": "safe", "command": "evil"})
    self.assertEqual({"cmd": "safe"}, out)

  def test_alias_does_not_bypass_gate(self):
    """别名归一化发生在门禁之前：改名不能绕过安全策略。"""
    args = self.core._normalize_args("run_command", {"command": "rm -rf /"})
    with self.assertRaises(Exception) as ctx:
      self.core.call_tool("run_command", args)
    self.assertIn("破坏性", str(ctx.exception),
           "走别名的危险命令没有被门禁拦住")

  def test_unknown_alias_passes_through(self):
    """未登记的键原样透传，由 schema 校验去判，不在别名层吞掉。"""
    out = self.core._normalize_args("fs_read", {"path": "a.txt"})
    self.assertEqual({"path": "a.txt"}, out)

  def test_alias_works_through_protocol(self):
    """同样必须走 handle()：验证别名在真实调用链上生效。

    只看 _normalize_args 的返回值是测了个函数，不是测了功能。
    """
    from unittest import mock
    import omegaforge.mcp_server as ms
    from omegaforge.tools.policy import Policy
    scope = {k: True for k in ms.MCP_SCOPE_DEFAULTS}
    # task_add 属个人数据写工具：作用域默认对外关闭，权限级别默认档
    # confirm 又要求先确认。本用例验的是**别名链路**，不是政策放行，
    # 所以两道都调开，再看别名有没有真正传到工具。
    with mock.patch.object(ms.McpScope, "load", lambda self: scope), \
        mock.patch.object(Policy, "mode", lambda self: "full"):
      resp = self.core.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "task_add",
              "arguments": {"content": "别名链路测试"}}})
    result = resp.get("result", {})
    self.assertNotEqual(True, result.get("isError"),
              f"走别名的调用被拒：{result}")
    text = result.get("content", [{}])[0].get("text", "")
    self.assertIn("别名链路测试", text, "别名参数没有真正传到工具")

  def test_no_alias_tool_untouched(self):
    out = self.core._normalize_args("kb_search", {"query": "折扣"})
    self.assertEqual({"query": "折扣"}, out)



if __name__ == "__main__":
  unittest.main(verbosity=2)
