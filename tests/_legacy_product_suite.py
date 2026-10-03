"""OmegaForge 产品套件测试：知识库 / Wiki / 任务 / 技能 / MCP / SuperAgent kit。

运行: python3 tests/_legacy_product_suite.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def check(name, fn):
  try:
    fn()
    RESULTS.append(True)
    print(f" ✅ {name}")
  except Exception as e:           # noqa: BLE001
    RESULTS.append(False)
    print(f" ❌ {name} → {type(e).__name__}: {str(e)[:140]}")


TMP = tempfile.mkdtemp(prefix="of_product_")
os.environ["OMEGAFORGE_HOME"] = TMP


def _confirm_blocks_kit_write():
  """默认权限级别（confirm 变更前确认）下，kit 的写工具必须被拦下。

  这是 enable_kit 缺少该约束时完全绕过门禁的回归守卫：注入的函数直接执行，
  用户设成"改文件前先问我"也不起作用。
  """
  from omegaforge.tools.policy import Policy
  from omegaforge.agent.super_agent import KitToolBlocked
  saved = Policy(TMP).mode()
  try:
    Policy(TMP).set_mode("confirm")
    _g = Genome(name="G2", mission_one_liner="m", source_fingerprint="f",
          system_prompt="s", tool_genes=["task_add(text)"],
          tools=["task_add"])
    _a = SuperAgent(_g, llm, TokenBank(10000)).enable_kit(TMP)
    try:
      _a.tool_impls["task_add"](json.dumps({"text": "不该写入"}))
    except KitToolBlocked:
      return True
    return False
  finally:
    Policy(TMP).set_mode(saved)

from omegaforge.memory.kb import KnowledgeBase     # noqa: E402
from omegaforge.memory.wiki import Wiki         # noqa: E402
from omegaforge.memory.tasks import Tasks        # noqa: E402
from omegaforge.skills.manager import SkillManager   # noqa: E402
from omegaforge.mcp_server import McpCore        # noqa: E402
from omegaforge.agent.super_agent import SuperAgent   # noqa: E402
from omegaforge.distill.genome import Genome      # noqa: E402
from omegaforge.llm.client import LLMClient       # noqa: E402
from omegaforge.core.errors import UserError      # noqa: E402
from omegaforge.core.budget import TokenBank      # noqa: E402

print("◆ 10. 知识库 KnowledgeBase")
db = KnowledgeBase(TMP)
check("10.1 写入与计数", lambda: (_ for _ in ()).throw(AssertionError())
   if not (db.add("项目约定", "所有回复必须用中文，简洁为主", type="note")
       and db.count() >= 1) else None)
db.add("Python GIL", "GIL 限制多线程并行执行字节码，CPU 密集用 multiprocessing")
check("10.2 中文检索命中", lambda: (_ for _ in ()).throw(AssertionError())
   if not db.search("回复 语言 偏好") or "项目约定" not in
   [r["title"] for r in db.search("回复语言")] else None)
check("10.3 类型过滤", lambda: (_ for _ in ()).throw(AssertionError())
   if any(d["type"] != "note" for d in db.all(type="note")) else None)
check("10.4 删除", lambda: (_ for _ in ()).throw(AssertionError())
   if not db.delete(db.all()[0]["id"]) else None)
check("10.5 持久化（重开实例）", lambda: (_ for _ in ()).throw(AssertionError())
   if KnowledgeBase(TMP).count() < 1 else None)

print("◆ 11. Wiki")
wiki = Wiki(TMP, kb=db)
wiki.save("omegaforge-intro", "OmegaForge 介绍",
     "# 介绍\n这是一个蒸馏 agent 的框架，详见 [[distill-engine]]")
wiki.save("distill-engine", "蒸馏引擎",
     "七步流水线，参考 [[omegaforge-intro|主页]]")
check("11.1 保存与读取", lambda: (_ for _ in ()).throw(AssertionError())
   if (wiki.get("omegaforge-intro") or {}).get("title") != "OmegaForge 介绍" else None)
check("11.2 双链与反链", lambda: (_ for _ in ()).throw(AssertionError())
   if "distill-engine" not in wiki.get("omegaforge-intro")["links"]
   or "omegaforge-intro" not in wiki.get("distill-engine")["backlinks"] else None)
check("11.3 搜索", lambda: (_ for _ in ()).throw(AssertionError())
   if "distill-engine" not in [r["slug"] for r in wiki.search("七步 流水线")] else None)
def _t114():
  # 契约变更（不是掩盖回归）：非法 slug 从 ValueError 升级为 UserError。
  # 原因——ValueError 会被 _classify 压成泛化的「请求内容有误，请检查后重试」，
  # 这句已经写好的中文规则说明被整句吞掉，用户看不出是地址格式不对。
  # 断言同步收紧：不只验"抛异常"，还要验中文文案真到达用户。
  try:
    wiki.save("Bad Slug!", "x", "b")
    raise AssertionError("expected UserError")
  except UserError as e:
    if "词条地址" not in str(e):
      raise AssertionError(f"文案不对: {e}")
check("11.4 非法 slug 拒绝", _t114)
check("11.5 同步进知识库索引", lambda: (_ for _ in ()).throw(AssertionError())
   if not db.search("蒸馏引擎 介绍") else None)

print("◆ 12. 任务 Tasks")
t = Tasks(TMP)
a = t.add("写蒸馏报告", 1)
b = t.add("整理书架", 3)
check("12.1 添加+优先级排序", lambda: (_ for _ in ()).throw(AssertionError())
   if [i["id"] for i in t.list()][0] != a["id"] else None)
check("12.2 完成", lambda: (_ for _ in ()).throw(AssertionError())
   if not t.complete(a["id"]) or a["id"] in
   [i["id"] for i in t.list("pending")] else None)
check("12.3 统计", lambda: (_ for _ in ()).throw(AssertionError())
   if t.stats() != {"total": 2, "pending": 1, "done": 1} else None)
def _t124():
  # 契约变更（同上）：空文本从英文 ValueError("task text required")
  # 升级为中文 UserError。CLI 缺少该约束时显示的是泛化的「请求内容有误」，
  # 用户填了空格却拿到系统故障级模糊提示。
  try:
    t.add("  ")
    raise AssertionError("expected UserError")
  except UserError as e:
    if "请填写任务内容" not in str(e):
      raise AssertionError(f"文案不对: {e}")
check("12.4 空文本拒绝", _t124)

print("◆ 13. 技能 Skills")
skill_dir = os.path.join(TMP, "my-skill")
os.makedirs(skill_dir)
open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8").write(
  "---\nname: code-review\ndescription: 按清单审查代码\nversion: 1.1\n---\n"
  "审查 {{context}} 的代码：1) 安全 2) 命名 3) 测试覆盖")
m = SkillManager(TMP)
check("13.1 安装", lambda: (_ for _ in ()).throw(AssertionError())
   if m.install(skill_dir).get("installed") != "code-review" else None)
check("13.2 列表只含元数据（渐进披露）", lambda: (_ for _ in ()).throw(AssertionError())
   if "{{context}}" in json.dumps(m.list(), ensure_ascii=False)
   or "1) 安全" in json.dumps(m.list(), ensure_ascii=False) else None)
check("13.3 调用返回正文+模板替换", lambda: (_ for _ in ()).throw(AssertionError())
   if "审查 app.py 的代码" not in m.invoke("code-review", "app.py") else None)
def _t134():
  try:
    m.invoke("nope")
    raise AssertionError("expected FileNotFoundError")
  except FileNotFoundError:
    pass
check("13.4 未安装技能报错", _t134)
def _t135():
  bad = os.path.join(TMP, "bad-skill")
  os.makedirs(bad)
  open(os.path.join(bad, "SKILL.md"), "w").write("no frontmatter here")
  try:
    m.install(bad)
    raise AssertionError("expected ValueError")
  except ValueError:
    pass
check("13.5 无 frontmatter 拒绝", _t135)

print("◆ 14. MCP 服务器（协议层）")
core = McpCore()
r = core.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
check("14.1 initialize 握手", lambda: (_ for _ in ()).throw(AssertionError())
   if r["result"]["serverInfo"]["name"] != "omegaforge" else None)
r = core.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})


def _t142():
  """契约已从"固定 19 个"改为"只列可用工具"。

  写死数量是坏测试：一旦新增工具或调整默认能力，它就在报假警，
  而真正该守的——"不可用却列出来"——反而没人看。故改为验不变量。
  """
  from omegaforge.mcp_server import _SYS_DISPATCH, _sys_available
  names = [t["name"] for t in r["result"]["tools"]]
  leaked = [n for n in names
       if n in _SYS_DISPATCH and not _sys_available(n)]
  if leaked:
    raise AssertionError(f"不可用却仍列出：{leaked}")
  if not names:
    raise AssertionError("工具列表为空，MCP 不可用")
  # 非系统工具必须全部在列，否则是过滤逻辑误杀。
  # 例外：个人数据的**写**工具（kb_add / task_add / memory_remember /
  # wiki_save…）默认对外关闭——外部 MCP 客户端不继承本机权限，写用户
  # 数据要显式授权。读工具不受此限（接 MCP 就是为了让人用知识库）。
  from omegaforge.tools.system_tools import MCP_SCOPE_DEFAULTS
  total = [t["name"] for t in McpCore.TOOLS]
  missing = [n for n in total
        if n not in _SYS_DISPATCH and n not in names
        and n not in MCP_SCOPE_DEFAULTS]
  if missing:
    raise AssertionError(f"非系统工具被误过滤：{missing}")
  leaked_write = [n for n in names if n in MCP_SCOPE_DEFAULTS]
  if leaked_write:
    raise AssertionError(f"未授权却列出了写工具：{leaked_write}")


check("14.2 tools/list 只列可用工具（不写死数量）", _t142)
# kb_add 属个人数据写工具，默认对外关闭：显式授权后再测写入链路。
from unittest import mock                  # noqa: E402
import omegaforge.mcp_server as _ms              # noqa: E402
_SCOPE_ALL = {k: True for k in _ms.MCP_SCOPE_DEFAULTS}
# 契约变更：外部 MCP 客户端写个人数据，除 scope 外还要过本机
# 权限级别；confirm 档需要弹窗确认，而 MCP 没有确认通道，因此必须**明确
# 拒绝**并给出可行动文案，不能假装成功。原先断言"15.3 直接写入成功"是
# 建立在旧契约（MCP 写入完全绕开权限级别）上的，现在改为先验拒绝、再验放行。
from omegaforge.tools.policy import Policy as _MCPPol     # noqa: E402

with mock.patch.object(_ms.McpScope, "load", lambda self: _SCOPE_ALL):
  r = core.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "kb_add",
                "arguments": {"title": "mcp 测试",
                       "text": "通过 mcp 写入"}}})
check("14.3 confirm 档下 MCP 写入被明确拒绝（不是假装成功）",
   lambda: (_ for _ in ()).throw(AssertionError())
   if (not r["result"].get("isError")
     or "确认" not in r["result"]["content"][0]["text"]) else None)

_MCPModeBefore = _MCPPol(TMP).mode()
_MCPPol(TMP).set_mode("auto_edit")
try:
  with mock.patch.object(_ms.McpScope, "load", lambda self: _SCOPE_ALL):
    r = core.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "kb_add",
                  "arguments": {"title": "mcp 测试",
                         "text": "通过 mcp 写入"}}})
  check("14.3b auto_edit 档下 MCP 写入放行",
     lambda: (_ for _ in ()).throw(AssertionError())
     if "id" not in json.loads(r["result"]["content"][0]["text"]) else None)
  with mock.patch.object(_ms.McpScope, "load", lambda self: _SCOPE_ALL):
    r = core.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "kb_search",
                  "arguments": {"query": "mcp 写入"}}})
  check("14.4 tools/call kb_search", lambda: (_ for _ in ()).throw(AssertionError())
     if not json.loads(r["result"]["content"][0]["text"])["results"] else None)
finally:
  _MCPPol(TMP).set_mode(_MCPModeBefore)
r = core.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "task_add", "arguments": {"text": "via mcp"}}})
r = core.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "task_list"}})
check("14.5 task via MCP", lambda: (_ for _ in ()).throw(AssertionError())
   if not json.loads(r["result"]["content"][0]["text"])["items"] else None)
r = core.handle({"jsonrpc": "2.0", "id": 7, "method": "nope"})
check("14.6 未知方法 -32601", lambda: (_ for _ in ()).throw(AssertionError())
   if r["error"]["code"] != -32601 else None)
r = core.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "task_add", "arguments": {"text": ""}}})
# 契约已更新：MCP 规范规定工具执行失败应返回 result + isError，
# 这样模型看得到原因并自我纠正（换参数/换工具）；JSON-RPC error 只用于
# 协议级失败，客户端会当成连接问题。缺少该约束时断言 -32000 是旧行为。
check("14.7 工具异常转 isError（非协议级 error）",
   lambda: (_ for _ in ()).throw(AssertionError())
   if "error" in r or not r.get("result", {}).get("isError") else None)

print("◆ 15. MCP 子进程烟雾测试（真实 stdio）")
env = dict(os.environ, OMEGAFORGE_HOME=os.path.join(TMP, "mcp_sub"))
proc = subprocess.Popen(
  [sys.executable, "-m", "omegaforge.mcp_server"],
  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
  stderr=subprocess.DEVNULL, env=env,
  cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
  init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
  lst = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
  out, _ = proc.communicate((init + "\n" + lst + "\n").encode(), timeout=30)
  lines = [json.loads(l) for l in out.decode().strip().split("\n") if l.strip()]
  check("15.1 子进程 initialize", lambda: (_ for _ in ()).throw(AssertionError())
     if lines[0]["result"]["serverInfo"]["name"] != "omegaforge" else None)
  def _t152():
    """子进程同样只列可用工具——过滤必须发生在服务器侧，
    不能只在进程内 McpCore 里生效而真实 stdio 通道漏掉。
    （缺少该约束时正因只测进程内，接通漏了也全绿。）"""
    from omegaforge.mcp_server import _SYS_DISPATCH
    names = [t["name"] for t in lines[1]["result"]["tools"]]
    leaked = [n for n in names if n in _SYS_DISPATCH]
    if leaked:
      raise AssertionError(f"子进程仍列出不可用系统工具：{leaked}")
    if not names:
      raise AssertionError("子进程工具列表为空")

  check("15.2 子进程 tools/list 只列可用工具", _t152)
except subprocess.TimeoutExpired:
  proc.kill()
  check("15.1 子进程 initialize", lambda: (_ for _ in ()).throw(AssertionError("timeout")))
  check("15.2 子进程 tools/list", lambda: (_ for _ in ()).throw(AssertionError("timeout")))

print("◆ 16. SuperAgent 个人工具包 enable_kit")
llm = LLMClient()
g = Genome(name="KitAgent", mission_one_liner="use kit", source_fingerprint="f",
      system_prompt="use tools", tool_genes=["kb_search(query)", "task_add(text)"],
      tools=["kb_search", "task_add"])
ag = SuperAgent(g, llm, TokenBank(50000)).enable_kit(TMP)
check("16.1 工具注入数量", lambda: (_ for _ in ()).throw(AssertionError())
   if len(ag.tool_impls) < 7 else None)
out = ag.tool_impls["kb_search"](json.dumps({"query": "mcp 写入"}))
check("16.2 kb_search 可调用", lambda: (_ for _ in ()).throw(AssertionError())
   if not json.loads(out).get("results") else None)
# 写工具（task_add / kb_add / memory_remember）现在**过门禁**：
# 默认是 confirm（变更前确认），medium 风险在这一档是 ask，所以必须先
# 放宽到 auto_edit 才能断言"能写入"。这不是测试迁就实现——门禁的产品
# 语义就是"改数据前先问我"，缺少该约束时这里完全绕过门禁才是 bug。
from omegaforge.tools.policy import Policy as _Pol     # noqa: E402
_POL_BEFORE = _Pol(TMP).mode()
_Pol(TMP).set_mode("auto_edit")
out = ag.tool_impls["task_add"](json.dumps({"text": "kit 任务"}))
check("16.3 task_add 可调用", lambda: (_ for _ in ()).throw(AssertionError())
   if not json.loads(out).get("id") else None)
check("16.3b confirm 下写工具需确认（门禁生效）",
   lambda: (_ for _ in ()).throw(AssertionError())
   if not _confirm_blocks_kit_write() else None)
out = ag.tool_impls["kb_search"]("not json")
check("16.4 非法参数不崩（防御）", lambda: (_ for _ in ()).throw(AssertionError())
   if "error" not in json.loads(out) and "results" not in json.loads(out) else None)

passed = sum(RESULTS)
print("\n" + "═" * 60)
print(f"产品套件结果: {passed}/{len(RESULTS)} PASS")
if passed != len(RESULTS):
  sys.exit(1)
print("全部产品功能验证通过 ✅")
shutil.rmtree(TMP, ignore_errors=True)
