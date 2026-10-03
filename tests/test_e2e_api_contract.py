#!/usr/bin/env python3
"""真后端 × 前端真实 payload 的**联调契约**守卫。

为什么必须有这一层：

  缺少该约束时所有契约检查都是"读代码核对字符串"。而 api.ts 里这些调用的返回
  类型全是 Record<string, unknown> —— **字段名写错在 TypeScript 里永远
  不报错**，构建照过、渲染照过，只有真发一次 HTTP 才暴露。

  联调验证（真实 HTTP，库里确有数据）抓到三处：
    kb/search   前端发 {query} / 后端读 q  → 搜"折扣"返回 []
    memory/recall 前端发 {query} / 后端读 q  → 检索恒返回 []
    tasks/done  前端发 {id}  / 后端读 task_id → 恒定 400「请填写：任务编号」

  三处的界面表现都是"点了没反应 / 提示一个不存在的输入框"，重试永远
  不会成功。

守卫分三层，互相兜底必须打破：

  A 端到端语义  用 **从 api.ts 解析出的真实键名** 打真后端，断言真的
          搜到东西 / 真的标记完成。用解析而非手写：手写等于把
          前端的错再抄一遍，撤前端修复时测试照样绿。
  B 契约一致性  前端键名必须等于后端 pick_first 的**首选**键名。
          后端为了兼容两个名字都认，所以撤掉前端修复时 A 仍绿
          ——只有 B 能抓到"前端发的是别名"。
  C 兼容不丢   旧键名（query / id）打后端仍要成功。防处理过头：不能
          为了契约干净把历史调用方一起打死。
  D 非空对照   搜一个不存在的词必须返回空。否则"恒返回全部"也能让 A 绿，
          A 就成了形式化。
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
  sys.path.insert(0, REPO)

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
API_TS = os.path.join(REPO, "frontend", "src", "lib", "api.ts")
SERVER_PY = os.path.join(REPO, "omegaforge", "server.py")


# ---------------------------------------------------------------- 解析 --

def _frontend_call(fn):
  """从 api.ts 解析 export const <fn> 的 (路径, payload 键名列表)。

  解析而非手写：手写的键名是"我以为前端发的"，撤前端修复时不会变，
  等于把缺陷抄进断言里。
  """
  src = open(API_TS, encoding="utf-8").read()
  m = re.search(r"export (?:const|function) " + fn + r"\b", src)
  if not m:
    raise AssertionError(f"api.ts 里找不到 {fn}")
  tail = src[m.start(): m.start() + 1500]
  # 泛型是 Record<string, unknown>，内含 '>'，用 [^>]* 会在嵌套处截断
  # （三个函数全部解析失败）。非贪婪 .*? + 紧跟 >( 才能跨过嵌套。
  m2 = re.search(r"post<.*?>\s*\(\s*'([^']+)'\s*,\s*\{([^{}]*)\}", tail, re.S)
  if not m2:
    raise AssertionError(f"{fn} 的 post 调用解析失败")
  path, body = m2.group(1), m2.group(2).strip()
  keys = re.findall(r"([A-Za-z_]\w*)\s*:", body)
  if not keys and re.fullmatch(r"[A-Za-z_]\w*", body):
    keys = [body]      # `{ q }` 简写
  return path, keys


def _backend_keys(endpoint):
  """从 server.py 解析该端点 pick_first 接受的键名（按优先级）。"""
  src = open(SERVER_PY, encoding="utf-8").read()
  # 同一端点在 do_GET 与 do_POST 各出现一次（只读分支用 ?q= 查询串，
  # 没有 pick_first）。只取第一个出现会落在 GET 分支上（kb/search
  # 与 memory/recall 两个都解析失败）。必须逐个窗口找。
  found = False
  for m in re.finditer(r'path == "' + re.escape(endpoint) + r'"', src):
    tail = src[m.start(): m.start() + 900]
    m2 = re.search(r'pick_first\(payload,\s*\(([^)]*)\)\)', tail, re.S)
    if m2:
      return [k for k in re.findall(r'"([^"]+)"', m2.group(1))]
    found = True
  if not found:
    raise AssertionError(f"server.py 里找不到 {endpoint}")
  raise AssertionError(f"{endpoint} 没有 pick_first，契约解析失败")


# ------------------------------------------------------------ 真后端 --

def _http(path, payload=None, timeout=30):
  data = json.dumps(payload).encode() if payload is not None else None
  req = urllib.request.Request(
    BASE + path, data=data, method="POST" if data else "GET",
    headers={"Content-Type": "application/json"} if data else {})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      return r.status, json.loads(r.read().decode("utf-8"))
  except urllib.error.HTTPError as e:
    return e.code, json.loads(e.read().decode("utf-8"))


class _Live(unittest.TestCase):
  """起真后端 + 临时数据目录。环境必须严格还原（本项目栽过多次）。"""

  @classmethod
  def setUpClass(cls):
    cls._saved_env = dict(os.environ)
    cls._home = "/tmp/e2e_contract_home"
    shutil.rmtree(cls._home, ignore_errors=True)
    os.makedirs(cls._home, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = cls._home
    os.environ["MOCK"] = "1"

    import omegaforge.server as srv
    from http.server import ThreadingHTTPServer
    cls._srv_module = srv
    cls._saved_home = {}
    for nm in ("RUNS", "CONVS", "USAGE", "TASKS", "KB", "WIKI", "JOBS",
          "MEMORY", "PROVIDERS", "SKILLS"):
      obj = getattr(srv, nm, None)
      if obj is not None and hasattr(obj, "home"):
        try:
          # 必须保存 _explicit（原始显式值，通常是 None = 跟随环境），
          # 不能保存 obj.home（getter 结果 = 已解析出的具体路径）。
          # ：存 getter 结果再赋回去，会把单例从
          # "跟随环境变量" 降级成 "钉死在某个具体路径"，
          # 后续测试写 OMEGAFORGE_HOME 时两边不一致 → 读到空数据。
          # 这是环境还原类失效在本项目的第 N 次复发。
          cls._saved_home[nm] = getattr(obj, "_explicit", None)
          obj.home = cls._home
        except Exception:
          pass
    cls.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
    time.sleep(0.5)

    # 种子：库里必须有可搜到的内容，否则"返回空"分不清是没数据还是查不到
    _http("/api/kb/add", {"title": "折扣策略",
               "text": "满200减30，会员可叠加", "type": "note"})
    _http("/api/memory/remember", {"fact": "用户偏好中文回复"})
    cls.task_id = _http("/api/tasks/add", {"text": "联调待办"})[1]["id"]

  @classmethod
  def tearDownClass(cls):
    try:
      cls.httpd.shutdown()
      cls.httpd.server_close()
    except Exception:
      pass
    for nm, h in cls._saved_home.items():
      try:
        getattr(cls._srv_module, nm).home = h
      except Exception:
        pass
    os.environ.clear()
    os.environ.update(cls._saved_env)
    shutil.rmtree(cls._home, ignore_errors=True)


# -------------------------------------------------------------- A 层 --

class FrontendPayloadHitsBackend(_Live):

  def test_kb_search_finds_seeded_doc(self):
    path, keys = _frontend_call("kbSearch")
    self.assertEqual(path, "/api/kb/search")
    st, body = _http(path, {keys[0]: "折扣"})
    self.assertEqual(st, 200)
    self.assertTrue(body.get("results"),
            f"前端键名 {keys} 打真后端搜不到种子条目：{body}")

  def test_memory_recall_finds_seeded_fact(self):
    path, keys = _frontend_call("recallMemory")
    self.assertEqual(path, "/api/memory/recall")
    st, body = _http(path, {keys[0]: "偏好"})
    self.assertEqual(st, 200)
    self.assertTrue(body.get("memories"),
            f"前端键名 {keys} 打真后端检索不到种子记忆：{body}")

  def test_task_done_completes(self):
    path, keys = _frontend_call("taskDone")
    self.assertEqual(path, "/api/tasks/done")
    st, body = _http(path, {keys[0]: self.task_id})
    self.assertEqual(st, 200, f"前端键名 {keys} 被拒：{body}")
    self.assertTrue(body.get("completed"))


# -------------------------------------------------------------- B 层 --

class ContractKeyIsCanonical(_Live):
  """前端必须发后端的**规范**键名，不能靠后端兼容兜着。"""

  def test_kb_search_key(self):
    _, keys = _frontend_call("kbSearch")
    self.assertEqual(keys[0], _backend_keys("/api/kb/search")[0])

  def test_memory_recall_key(self):
    _, keys = _frontend_call("recallMemory")
    self.assertEqual(keys[0], _backend_keys("/api/memory/recall")[0])

  def test_task_done_key(self):
    _, keys = _frontend_call("taskDone")
    self.assertEqual(keys[0], _backend_keys("/api/tasks/done")[0])


# -------------------------------------------------------------- C 层 --

class LegacyKeyStillAccepted(_Live):
  """兼容不能丢：历史调用方（含旧格式前端）发的别名仍要成功。"""

  def test_kb_search_query(self):
    st, body = _http("/api/kb/search", {"query": "折扣"})
    self.assertEqual(st, 200)
    self.assertTrue(body.get("results"), body)

  def test_memory_recall_query(self):
    st, body = _http("/api/memory/recall", {"query": "偏好"})
    self.assertEqual(st, 200)
    self.assertTrue(body.get("memories"), body)

  def test_task_done_id(self):
    tid = _http("/api/tasks/add", {"text": "别名兼容待办"})[1]["id"]
    st, _ = _http("/api/tasks/done", {"id": tid})
    self.assertEqual(st, 200)


# -------------------------------------------------------------- D 层 --

class SearchIsNotAlwaysNonEmpty(_Live):
  """防形式化：搜不存在的词必须返回空，否则 A 层"非空"断言毫无意义。"""

  def test_kb_search_miss(self):
    _, body = _http("/api/kb/search", {"q": "绝无此词zzz"})
    self.assertEqual(body.get("results"), [])

  def test_memory_recall_miss(self):
    _, body = _http("/api/memory/recall", {"q": "绝无此词zzz"})
    self.assertEqual(body.get("memories"), [])


if __name__ == "__main__":
  unittest.main(verbosity=2)
