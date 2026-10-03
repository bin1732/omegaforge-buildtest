#!/usr/bin/env python3
"""全端点联调契约守卫（前端真实键名 × 真后端真 HTTP）。

为什么必须有这一层
------------------
只联调了 3 个端点（kb.search / memory.recall / tasks.done），本层
把 21 个 POST + 16 个 GET 全部过一遍。本次改动验证新增抓到：

  GET /api/wiki/page 前端发 ?name=<slug> / 后端读 slug
    → 列表里明明列着的词条，点开恒定 404「未找到该词条」。

与三处的界面表现完全同型（"点了打不开 / 提示一个不存在的输入
框"，重试永远不会成功），但发生在 **GET 查询串** 而非 POST body 上——
说明这类缺陷不是某一处笔误，而是一类需要独立守卫的缺口。

守卫分五层
----------
0 解析器自洽 解析器自身必须先被验证。验证（本次改动）：解析窗口不截断会
       越界吃到**后一个函数**的 payload，kbAdd 解析成 ['q']、
       taskAdd 解析成 ['task_id']、sendChat 解析成 ['title']。
       用这种解析器写的守卫会去验证错误的键名，全绿但什么都没验到。
       这是"假守卫"在本项目的新形态，必须单独钉住。
A 必需字段覆盖 后端 need() 的位置参数（真实必需字段）必须都在前端键名里。
       能自动发现"前端漏发 → 恒定 400"。
B GET 查询串  前端查询串键名必须等于后端读取的键名。
C 端到端    用解析出的前端键名构造合法值打真后端，断言不是"字段缺失类"
       错误（400 请填写 / 404 接口不存在 / 500 操作失败）。
D 语义对照   搜存在的词必须非空、搜不存在的词必须为空。否则 C 层会被
       "恒返回全部"或"恒返回空"骗过。
"""

import base64
import glob
import json
import os
import re
import shutil
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
  sys.path.insert(0, REPO)

PORT = 8801
BASE = f"http://127.0.0.1:{PORT}"
API_TS = os.path.join(REPO, "frontend", "src", "lib", "api.ts")
SERVER_PY = os.path.join(REPO, "omegaforge", "server.py")
PAGE_GLOB = os.path.join(REPO, "frontend", "src", "pages", "*.tsx")


# ------------------------------------------------------------ 解析工具 --

def _close(s, i):
  """从 s[i]=='{' 起返回匹配的 '}' 索引。"""
  d = 0
  for j in range(i, len(s)):
    if s[j] == "{":
      d += 1
    elif s[j] == "}":
      d -= 1
      if d == 0:
        return j
  return -1


def _keys(inner):
  """顶层键名。必须深度感知：分隔符只在 depth==0 生效，否则
  `models?: { main; fast; judge }` 里的 fast/judge 会被当成顶层键
  （验证：applyProvider 多出 fast、judge）。"""
  parts, d, cur = [], 0, ""
  for ch in inner:
    if ch in "{[(":
      d += 1
    elif ch in "}])":
      d -= 1
    if d == 0 and ch in ",;\n":
      parts.append(cur)
      cur = ""
    else:
      cur += ch
  parts.append(cur)
  ks = []
  for p in parts:
    p = p.strip()
    if not p:
      continue
    m = re.match(r"([A-Za-z_]\w*)\s*\??\s*:", p)
    if m:
      ks.append(m.group(1))
    elif re.fullmatch(r"[A-Za-z_]\w*", p):
      ks.append(p)
  return ks


def _api_src():
  return open(API_TS, encoding="utf-8").read()


def _pages():
  return {os.path.basename(f): open(f, encoding="utf-8").read()
      for f in sorted(glob.glob(PAGE_GLOB))}


def _win(src, start, limit=1200):
  """窗口必须在下一个 `\nexport ` 处截断。

  验证（本次改动）：不截断时 kbAdd 解析出 ['q']、taskAdd 解析出 ['task_id']
  ——1500 字符窗口越界吃到了后面函数的 post 调用。"""
  nxt = src.find("\nexport ", start + 1)
  return src[start:min(nxt if nxt != -1 else start + limit, start + limit)]


def fe_keys(fn):
  """解析前端对某端点实际发送的键名，返回 (来源, 路径, 键名列表)。"""
  src = _api_src()
  pages = _pages()
  allpg = "".join(pages.values())
  m = re.search(r"\nexport (?:const|function) " + fn + r"\b", src)
  if not m:
    return ("MISSING", "", [])
  w = _win(src, m.start())
  mp = re.search(r"post<.*?>\s*\(\s*'([^']+)'\s*,", w, re.S)
  if not mp:
    return ("no-post", "", [])
  path = mp.group(1)
  rest = w[mp.end():]
  pm = re.match(r"\s*(\{|\w+)", rest)
  if pm and pm.group(1) == "{":
    i = rest.index("{")
    j = _close(rest, i)
    ks = _keys(rest[i + 1:j])
    if ks:
      return ("literal", path, ks)
  ma = re.search(r"export function " + fn + r"\s*\(\s*\w+\s*:\s*\{", w, re.S)
  if ma:
    i = w.index("{", ma.start())
    j = _close(w, i)
    return ("annotation", path, _keys(w[i + 1:j]))
  for m4 in re.finditer(r"\b" + fn + r"\(\s*\{", allpg):
    i = allpg.index("{", m4.start())
    j = _close(allpg, i)
    ks = _keys(allpg[i + 1:j])
    if ks:
      return ("callsite", path, ks)
  # 变量构造：startDistill(payload) → 找该变量的键名
  m5 = re.search(r"\b" + fn + r"\(\s*([A-Za-z_]\w*)\s*\)", allpg)
  if m5:
    var = m5.group(1)
    ks = []
    for txt in pages.values():
      # 注意 `const payload: Record<string, unknown> = {` —— 类型注解
      # 在变量名与 `=` 之间，正则必须容许（漏掉 source_prompt/task）
      for m6 in re.finditer(r"\b" + var + r"\b\s*(?::[^=]+)?=\s*\{", txt):
        i = txt.index("{", m6.start())
        j = _close(txt, i)
        ks += _keys(txt[i + 1:j])
      for m7 in re.finditer(r"\b" + var + r"\.([A-Za-z_]\w*)\s*=", txt):
        ks.append(m7.group(1))
    if ks:
      return ("var", path, sorted(set(ks)))
  return ("unknown", path, [])


def be_required(path):
  """后端 need() 的**位置**参数 = 真实必需字段（排除 key="中文" 的说明项）。"""
  src = open(SERVER_PY, encoding="utf-8").read()
  for m in re.finditer(r'path == "' + re.escape(path) + r'"', src):
    w = src[m.start(): m.start() + 900]
    # 窗口必须在下一个 elif path 处截断，否则会吃到后面端点的 need
    # （/api/tasks/done 解析出 ['技能包路径']，那是 skills/install 的）
    nxt = re.search(r'elif path ==|elif path\.startswith', w[10:])
    if nxt:
      w = w[:10 + nxt.start()]
    mn = re.search(r"\bneed\(([^)]*)\)", w, re.S)
    if not mn:
      continue
    arg = mn.group(1)
    head = re.split(r"[A-Za-z_]\w*\s*=\s*[\"']", arg)[0]
    ks = [k for k in re.findall(r'"([^"]+)"', head)]
    if ks:
      return ks
  return []


def be_query_keys(path):
  """后端 GET 路由读取的查询串键名。"""
  src = open(SERVER_PY, encoding="utf-8").read()
  for m in re.finditer(r'path == "' + re.escape(path) + r'"', src):
    w = src[m.start(): m.start() + 600]
    nxt = re.search(r'elif path ==|elif path\.startswith', w[10:])
    if nxt:
      w = w[:10 + nxt.start()]
    ks = re.findall(r'qs\.get\("([^"]+)"', w)
    if ks:
      return ks
  raise AssertionError(f"{path} 未读取任何查询串键名")


def fe_query_keys(fn):
  """前端 GET 调用实际拼的查询串键名。"""
  src = _api_src()
  m = re.search(r"\nexport (?:const|function) " + fn + r"\b", src)
  if not m:
    raise AssertionError(f"api.ts 里找不到 {fn}")
  w = _win(src, m.start())
  return re.findall(r"[?&]([a-z_]\w*)=", w)


# ------------------------------------------------------------ 真后端 --

class _Live(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls._saved_env = dict(os.environ)
    cls._home = "/tmp/e2e_full_home"
    shutil.rmtree(cls._home, ignore_errors=True)
    os.makedirs(cls._home, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = cls._home
    os.environ["MOCK"] = "1"

    import omegaforge.server as srv
    from http.server import ThreadingHTTPServer
    cls._srv = srv
    cls._saved_home = {}
    for nm in ("RUNS", "CONVS", "USAGE", "TASKS", "KB", "WIKI", "JOBS",
          "MEMORY", "PROVIDERS", "SKILLS"):
      obj = getattr(srv, nm, None)
      if obj is not None and hasattr(obj, "home"):
        try:
          # 必须保存 _explicit（通常是 None = 跟随环境变量），不能
          # 保存 obj.home（getter 结果 = 具体路径），否则会把单例
          # 从"跟随环境"降级成"钉死路径"（本项目栽过多次）。
          cls._saved_home[nm] = getattr(obj, "_explicit", None)
          obj.home = cls._home
        except Exception:
          pass
    cls.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
    time.sleep(0.6)

    # ---- 真实素材：技能包、wav、种子数据 ----
    cls.skill_dir = "/tmp/e2e_full_skill"
    shutil.rmtree(cls.skill_dir, ignore_errors=True)
    os.makedirs(cls.skill_dir, exist_ok=True)
    open(os.path.join(cls.skill_dir, "SKILL.md"), "w",
       encoding="utf-8").write(
      "---\nname: e2e-skill\ndescription: 联调用技能\ntype: tool\n---\n\n# 用法\n\n联调。\n")
    wav = "/tmp/e2e_full.wav"
    with wave.open(wav, "wb") as w:
      w.setnchannels(1)
      w.setsampwidth(2)
      w.setframerate(16000)
      w.writeframes(b"\x00\x00" * 160)
    cls.audio_b64 = base64.b64encode(open(wav, "rb").read()).decode()

    _http("/api/kb/add", {"title": "折扣策略", "text": "满200减30"})
    _http("/api/memory/remember", {"fact": "用户偏好中文回复"})
    _http("/api/wiki/save", {"slug": "probe", "title": "联调", "body": "内容"})
    cls.task_id = _http("/api/tasks/add", {"text": "联调待办"})[1]["id"]
    cls.conv_id = _http("/api/conversations/new", {"title": "联调"})[1]["id"]

  @classmethod
  def tearDownClass(cls):
    try:
      cls.httpd.shutdown()
      cls.httpd.server_close()
    except Exception:
      pass
    for nm, h in cls._saved_home.items():
      try:
        getattr(cls._srv, nm).home = h
      except Exception:
        pass
    os.environ.clear()
    os.environ.update(cls._saved_env)
    shutil.rmtree(cls._home, ignore_errors=True)


def _http(path, payload=None, timeout=60, method=None):
  data = json.dumps(payload).encode() if payload is not None else None
  m = method or ("POST" if data else "GET")
  req = urllib.request.Request(
    BASE + path, data=data, method=m,
    headers={"Content-Type": "application/json"} if data else {})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      raw = r.read().decode("utf-8", "replace")
      try:
        return r.status, json.loads(raw)
      except Exception:
        return r.status, {"_raw": raw[:200]}
  except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8", "replace")
    try:
      return e.code, json.loads(raw)
    except Exception:
      return e.code, {"_raw": raw[:200]}


# 合法值表：键名来自解析，值只是一组合法样例。
# name 在不同端点含义不同（供应商名 / 技能名 / 工具名），必须按端点区分——
# （）：统一给技能名会让 /api/providers/apply 报"未找到该模型供应商"。
def _value(path, k, cls):
  if k == "name" and path.startswith("/api/providers/"):
    return "openai"
  return {
    "q": "折扣",
    "task_id": cls.task_id,
    "id": cls.task_id,
    "conversation_id": cls.conv_id,
    "title": "联调标题",
    "text": "联调内容",
    "body": "联调正文",
    "slug": "probe2",
    "fact": "联调事实",
    "name": "e2e-skill",
    "path": cls.skill_dir,
    "message": "你好",
    "model": "auto",
    "priority": 2,
    "source_prompt": "你是助手",
    "task": "联调任务",
    "budget": 20000,
    "api_key": "sk-test",
    "base_url": "https://api.openai.com/v1",
    "models": {"main": "gpt-4o-mini"},
    "mode": "confirm",
    "arguments": {"path": "."},
    "audio_b64": cls.audio_b64,
    "speed": 1.0,
    "context": "x",
    "type": "note",
    "tags": ["联调"],
  }.get(k, "x")


# ---------------------------------------------------------- 0 解析器 --

class ParserIsSane(unittest.TestCase):
  """解析器自身必须先被验证。

  验证（本次改动）：窗口不截断会让 kbAdd 解析成 ['q']（越界吃到 kbSearch 的
  请求体）。用这种解析器写的守卫会去验证错误的键名——全绿，但什么都没
  验到。这一层是防止联调守卫整体沦为形式化。
  """

  # 不带 post 的纯 GET 函数（得出，见 test_window_never_overruns）
  GET_FNS = ["fetchConversations", "fetchGenome", "fetchJob",
        "fetchPermissions", "fetchProviderModels", "fetchProviders",
        "fetchReport", "fetchRuns", "fetchStatus", "fetchUsage",
        "kbList", "skillsList", "streamChat", "tasksList",
        "voiceStatus", "wikiList", "wikiPage"]

  def test_kb_add_not_confused_with_kb_search(self):
    self.assertEqual(fe_keys("kbAdd")[2], ["title", "text"])

  def test_task_add_not_confused_with_task_done(self):
    self.assertEqual(fe_keys("taskAdd")[2], ["text", "priority"])

  def test_send_chat_not_confused_with_new_conversation(self):
    self.assertEqual(fe_keys("sendChat")[2], ["message", "conversation_id"])

  def test_kb_search(self):
    self.assertEqual(fe_keys("kbSearch")[2], ["q"])

  def test_task_done(self):
    self.assertEqual(fe_keys("taskDone")[2], ["task_id"])

  def test_start_distill_var_construction(self):
    src, path, ks = fe_keys("startDistill")
    self.assertEqual(path, "/api/distill")
    self.assertIn("source_prompt", ks)
    self.assertIn("task", ks)

  def test_apply_provider_nested_not_flattened(self):
    src, path, ks = fe_keys("applyProvider")
    self.assertEqual(path, "/api/providers/apply")
    self.assertIn("name", ks)
    self.assertIn("models", ks)
    # 嵌套的 main/fast/judge 属于 models，不该出现在顶层
    self.assertNotIn("judge", ks)

  def test_backend_required_not_confused_across_endpoints(self):
    # tasks/done 没有 need()，窗口越界会解析出 skills/install 的字段
    self.assertEqual(be_required("/api/tasks/done"), [])
    self.assertEqual(be_required("/api/kb/add"), ["title", "text"])

  def test_window_never_overruns_into_next_export(self):
    """窗口必须在下一个函数前收住——这条缺少该约束时没有任何用例覆盖。

    ：把 _win 的截断撤掉后，17 个函数的解析结果整体串台
    （fetchRuns 拿到 /api/chat、wikiPage 拿到 /api/wiki/save、
    kbList 拿到 /api/kb/add、fetchUsage 拿到 /api/providers/apply……）。
    而 POST 函数走 fe_keys、GET 函数另走 fe_query_keys，两边互不覆盖，
    这 17 处串台一个都发现不了——测试照样全绿。
    """
    src = _api_src()
    bad = []
    for m in re.finditer(r"\nexport (?:const|function) (\w+)", src):
      w = _win(src, m.start())
      if "\nexport " in w[1:]:
        bad.append(m.group(1))
    self.assertFalse(bad, "窗口越界，解析会串到下一个函数：\n "
             + ", ".join(bad))

  def test_get_functions_parse_as_no_post(self):
    """上一条查窗口，这一条查结果：GET 函数不得解析出 post 路径。

    名单由得出（撤掉截断后从 no-post 变成带路径的那 17 个）。
    两条并存是因为它们判据不同：一条是动态判据、一条是不变式，
    只留一条都有可能被"换个写法"绕过。
    """
    for fn in self.GET_FNS:
      src, path, ks = fe_keys(fn)
      self.assertEqual(
        src, "no-post",
        f"{fn} 是 GET，却解析出 post 路径 {path}——窗口越界吃到了"
        f"别的函数")


# --------------------------------------------------- A 必需字段覆盖 --

class RequiredFieldsAreSent(_Live):
  """后端 need() 的必需字段，前端必须都发了，否则恒定 400。"""

  CASES = [("kbAdd", "/api/kb/add"),
       ("rememberMemory", "/api/memory/remember"),
       ("wikiSave", "/api/wiki/save"),
       ("skillInstall", "/api/skills/install"),
       ("skillInvoke", "/api/skills/invoke"),
       ("taskAdd", "/api/tasks/add"),
       ("applyProvider", "/api/providers/apply")]

  def test_required_fields_present(self):
    for fn, path in self.CASES:
      src, fpath, ks = fe_keys(fn)
      self.assertEqual(fpath, path, f"{fn} 解析出的端点不对")
      need = be_required(path)
      missing = [k for k in need if k not in ks]
      self.assertFalse(missing,
               f"{fn} → {path} 漏发后端必需字段 {missing}"
               f"（前端发 {ks}）")


# ------------------------------------------------------- B GET 查询串 --

class QueryStringContract(unittest.TestCase):
  """前端拼的查询串键名必须等于后端读的键名。

  验证（本次改动）：wikiPage 发 ?name=，后端只读 slug → 列表里明明列着的
  词条点开恒定 404。providers/models 的 name 是对的，一并钉住防回退。
  """

  def test_wiki_page_uses_slug(self):
    self.assertIn("slug", fe_query_keys("wikiPage"))
    self.assertEqual(fe_query_keys("wikiPage")[0],
             be_query_keys("/api/wiki/page")[0])

  def test_providers_models_uses_name(self):
    self.assertEqual(fe_query_keys("fetchProviderModels")[0],
             be_query_keys("/api/providers/models")[0])


# ------------------------------------------------------------ C 端到端 --

class AllPostEndpointsAcceptFrontendKeys(_Live):
  """用解析出的前端键名打真后端，不得出现"字段缺失类"错误。"""

  CASES = ["kbSearch", "recallMemory", "taskDone", "newConversation",
       "setConversationModel", "deleteConversation", "kbAdd",
       "rememberMemory", "wikiSave", "skillInstall", "skillInvoke",
       "taskAdd", "sendChat", "startDistill", "applyProvider",
       "testProvider"]

  def test_endpoints_accept(self):
    bad = []
    for fn in self.CASES:
      src, path, ks = fe_keys(fn)
      if not ks:
        continue
      payload = {k: _value(path, k, self) for k in ks}
      if path == "/api/chat":
        # 不能沿用 cls.conv_id：本用例前面的 deleteConversation 已把它
        # 删掉，chat 会报"未找到该对话"（测试自身的顺序依赖）。
        payload["conversation_id"] = _http(
          "/api/conversations/new", {"title": "chat载体"})[1]["id"]
      st, body = _http(path, payload)
      if st != 200:
        bad.append(f"{fn} → {path} 发 {ks} → {st} {body}")
    self.assertFalse(bad, "前端键名打真后端被拒：\n " + "\n ".join(bad))

  def test_wiki_page_opens_saved_entry(self):
    """抓到的缺陷：列表里列着，点开却 404。"""
    key = fe_query_keys("wikiPage")[0]
    st, body = _http(f"/api/wiki/page?{key}=probe")
    self.assertEqual(st, 200, f"?{key}=probe 打不开已存词条：{body}")
    self.assertEqual(body.get("slug"), "probe")

  def test_wiki_page_legacy_name_still_works(self):
    """兼容不能丢：旧调用方发 ?name= 仍要打得开。"""
    st, body = _http("/api/wiki/page?name=probe")
    self.assertEqual(st, 200, f"?name= 别名被拒：{body}")


# ------------------------------------------------------------ D 语义 --

class SemanticsNotAlwaysEmpty(_Live):
  """防形式化：搜不到的词必须返回空，否则 C 层的 200 毫无意义。"""

  def test_kb_search_hit_and_miss(self):
    hit = _http("/api/kb/search", {"q": "折扣"})[1].get("results")
    miss = _http("/api/kb/search", {"q": "绝无此词zzz"})[1].get("results")
    self.assertTrue(hit, "种子条目搜不到")
    self.assertEqual(miss, [], "搜不存在的词却返回了东西")

  def test_recall_hit_and_miss(self):
    hit = _http("/api/memory/recall", {"q": "偏好"})[1].get("memories")
    miss = _http("/api/memory/recall", {"q": "绝无此词zzz"})[1].get("memories")
    self.assertTrue(hit, "种子记忆检索不到")
    self.assertEqual(miss, [])

  def test_wiki_page_missing_entry_is_404(self):
    # 中文必须 urlencode：http.client 用 ascii 编码请求行，裸中文直接
    # UnicodeEncodeError（测试自己的错，不是产品问题）
    st, _ = _http("/api/wiki/page?slug=" + urllib.parse.quote("绝无此词条zzz"))
    self.assertEqual(st, 404, "不存在的词条却返回了内容")


if __name__ == "__main__":
  unittest.main(verbosity=2)
