"""server.py 路由层契约：请求体上限、跨域来源、错误文案统一。

这一层是**所有界面的唯一入口**，也是唯一暴露在浏览器里的一层。前面各章
修的都在被调用的函数里；这里守的是请求还没进函数之前的那段路。

为什么必须起真实 HTTP 服务来测（而不是直接调 Handler 的方法）：
本次改动三个问题**全部只在真实 HTTP 层才存在**——Content-Length 由客户端
提供、Origin 由浏览器附加、连接被断开表现为 RemoteDisconnected 而不是
一个 500。只测函数等于测了个寂寞：函数层面全绿，线上照样事故。
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# HOME 由 tests/conftest.py 在 import 期统一钉到隔离目录（conftest 最先被
# import，因此 server 在此处 import 时看到的已是隔离 HOME）。
# 原先各文件自己写 os.environ 的结果是：谁先被 import，整个会话的 HOME
# 就是谁的——由文件名字母序决定，而不是由用例决定。
from omegaforge.server import Handler, MAX_BODY_BYTES # noqa: E402


def _start() -> tuple:
  srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
  threading.Thread(target=srv.serve_forever, daemon=True).start()
  return srv, srv.server_address[1]


class _Harness:
  """按用例起独立服务，避免端口与 HOME 交叉污染。"""

  def setUp(self) -> None:
    self._snap = os.environ.get("OMEGAFORGE_HOME")
    os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_h_")
    self.srv, self.port = _start()
    self.addCleanup(self.srv.shutdown)
    self.addCleanup(self._restore)

  def _restore(self) -> None:
    if self._snap is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = self._snap

  def raw(self, method, path, body=None, headers=None, timeout=8):
    """发一条原始请求，返回 (status, headers, body_text)。

    连接被直接断开时返回 ("EXC", {}, 描述)——这正是要捕获的事故形态。
    """
    try:
      conn = http.client.HTTPConnection("127.0.0.1", self.port,
                       timeout=timeout)
      conn.request(method, path, body=body, headers=headers or {})
      r = conn.getresponse()
      data = r.read().decode("utf-8", "replace")
      conn.close()
      return r.status, dict(r.headers), data
    except Exception as e:            # noqa: BLE001
      return "EXC", {}, f"{type(e).__name__}: {e}"

  def post_json(self, path, payload, origin=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json",
        "Content-Length": str(len(data))}
    if origin:
      hdrs["Origin"] = origin
    return self.raw("POST", path, data, hdrs)

  def get(self, path, origin=None):
    hdrs = {"Origin": origin} if origin else {}
    return self.raw("GET", path, None, hdrs)


class TestContentLengthGuard(_Harness, unittest.TestCase):
  """Content-Length 是客户端给的，原实现直接 int() + read(n)。

  验证三种后果，没有一种是"报错"：
   "abc" / "12.5" -> ValueError 逃出 try（只兜 JSONDecodeError），
             连接被直接断开，客户端收到网络错误而非 HTTP 400
   "-1"      -> read(-1) 读到 EOF，请求永久挂起
   "999999999999" -> 预分配约 1TB，验证 MemoryError，单个请求即可打爆进程
  """

  ROUTE = "/api/conversations/new"

  def test_non_numeric_length_returns_400_not_dropped_connection(self):
    for bad in ("abc", "12.5", "", "1e3"):
      with self.subTest(length=bad):
        st, _, body = self.raw("POST", self.ROUTE, '{"title":"t"}',
                    {"Content-Length": bad})
        self.assertNotEqual("EXC", st,
                  f"Content-Length={bad!r} 导致连接被断开：{body}")
        self.assertEqual(400, st, f"Content-Length={bad!r} 应返回 400")

  def test_negative_length_does_not_hang(self):
    st, _, body = self.raw("POST", self.ROUTE, '{"title":"t"}',
                {"Content-Length": "-1"}, timeout=6)
    self.assertNotEqual("EXC", st, f"负数 Content-Length 导致挂起：{body}")
    self.assertEqual(400, st)

  def test_huge_length_is_capped_not_allocated(self):
    """封顶必须在 read() 之前：先分配再判断本身就已中招。"""
    st, _, body = self.raw("POST", self.ROUTE, "{}",
                {"Content-Length": "999999999999"})
    self.assertNotEqual("EXC", st, f"超大 Content-Length 打爆了进程：{body}")
    self.assertEqual(413, st)
    self.assertIn("过大", body)

  def test_cap_matches_declared_constant(self):
    """上限必须是常量本身，不能是写死在文案里的数字。"""
    st, _, body = self.raw("POST", self.ROUTE, "{}",
                {"Content-Length": str(MAX_BODY_BYTES + 1)})
    self.assertEqual(413, st)
    self.assertTrue(MAX_BODY_BYTES > 0)

  def test_legitimate_request_still_works(self):
    """不能为了防攻击把正常请求一起挡掉。"""
    st, _, body = self.post_json("/api/conversations/new", {"title": "正常"})
    self.assertEqual(200, st, f"正常请求被限流误伤：{body}")

  def test_missing_length_defaults_to_zero(self):
    st, _, _ = self.raw("POST", self.ROUTE, None, {})
    self.assertNotEqual("EXC", st)


class TestCorsOriginGuard(_Harness, unittest.TestCase):
  """响应头写死 ACAO: * —— 用户浏览的任意网页都能读本机数据。

  缺少该约束时：Origin: https://evil.example 下 GET /api/kb/list 返回
  200 + ACAO=*，知识库、对话、待办、任务、审计全部可读。GET 不触发预检，
  所以浏览器会原样把内容交给恶意页面。只绑 127.0.0.1 挡不住这件事——
  浏览器是在用户这一侧发起的。
  """

  SENSITIVE = ["/api/kb/list", "/api/conversations", "/api/tools/audit",
         "/api/tasks/list", "/api/runs"]

  def test_foreign_origin_cannot_read(self):
    for origin in ("https://evil.example", "http://attacker.io", "null"):
      for path in self.SENSITIVE:
        with self.subTest(origin=origin, path=path):
          st, hdrs, _ = self.get(path, origin=origin)
          self.assertEqual(403, st,
                   f"{origin} 仍能访问 {path}（status={st}）")
          self.assertIsNone(hdrs.get("Access-Control-Allow-Origin"),
                   f"{origin} 访问 {path} 仍拿到 ACAO")

  def test_local_origins_allowed_and_echoed(self):
    """前端自身的来源必须放行，否则整个界面用不了。

    vite dev = http://localhost:5173；Tauri v2 生产壳在 Win/Linux =
    http://tauri.localhost，macOS = tauri://localhost。
    """
    for origin in ("http://localhost:5173", "http://tauri.localhost",
            "tauri://localhost", "http://127.0.0.1:5173"):
      with self.subTest(origin=origin):
        st, hdrs, _ = self.get("/api/kb/list", origin=origin)
        self.assertEqual(200, st, f"前端来源被误伤：{origin}")
        self.assertEqual(origin,
                 hdrs.get("Access-Control-Allow-Origin"),
                 f"未回显来源，前端会被同源策略拦：{origin}")

  def test_no_origin_still_works(self):
    """无 Origin（curl / 本套件 / 非浏览器客户端）不受影响。

    这类调用方不受同源策略约束，把它们一起挡掉是纯损失。
    """
    st, _, _ = self.get("/api/kb/list")
    self.assertEqual(200, st)

  def test_write_from_foreign_origin_blocked_even_as_simple_request(self):
    """写不能只靠"不发 ACAO"。

    Content-Type: text/plain 属 CORS 简单请求，不触发预检，恶意页面
    照样能 POST 纯文本过来。所以来源不可信时直接拒绝。
    """
    # 用 Content-Length: 0 发：服务端在**读体之前**就 403（不读体即关闭
    # 连接），带体会让客户端收到 RemoteDisconnected 而看不到 403——
    # 那是测试形态的问题，不是产品问题（浏览器发小 body 不会有影响）。
    st, _, _ = self.raw("POST", "/api/kb/add", None,
              {"Content-Type": "text/plain",
               "Content-Length": "0",
               "Origin": "https://evil.example"})
    self.assertEqual(403, st)


class TestRouteErrorShape(_Harness, unittest.TestCase):
  """所有路由在脏入参下不得返回 500 或断开连接。"""

  GETS = ["/api/status", "/api/usage", "/api/tools/permissions",
      "/api/tools/policy", "/api/tools/audit", "/api/providers",
      "/api/providers/models", "/api/personas", "/api/conversations",
      "/api/kb/search", "/api/kb/list", "/api/wiki/list",
      "/api/wiki/page", "/api/tasks/list", "/api/skills/list",
      "/api/memory/recall", "/api/runs", "/api/genome/zz",
      "/api/jobs/zz", "/api/voice/status", "/nope"]

  POSTS = ["/api/conversations/new", "/api/conversations/delete",
       "/api/conversations/model", "/api/voice/tts", "/api/voice/asr",
       "/api/tools/permissions", "/api/tools/policy", "/api/kb/add",
       "/api/wiki/save", "/api/tasks/add", "/api/tasks/done",
       "/api/memory/remember", "/api/skills/install",
       "/api/skills/invoke", "/api/providers/apply"]

  def test_get_routes_never_500(self):
    for p in self.GETS:
      with self.subTest(path=p):
        st, _, body = self.get(p)
        self.assertNotEqual("EXC", st, f"{p} 连接被断开：{body}")
        self.assertNotEqual(500, st, f"{p} 返回 500：{body[:120]}")

  def test_post_routes_never_500_on_dirty_payload(self):
    for p in self.POSTS:
      for payload in ({}, {"a": None, "b": 1, "c": "x"},
              {"a": [1, 2]}, {"a": {"b": None}}):
        with self.subTest(path=p, payload=payload):
          st, _, body = self.post_json(p, payload)
          self.assertNotEqual("EXC", st,
                    f"{p} 连接被断开：{body}")
          self.assertNotEqual(500, st,
                    f"{p} 脏入参返回 500：{body[:120]}")

  def test_error_body_is_json_with_chinese_message(self):
    for p in ("/nope", "/api/genome/zz"):
      st, _, body = self.get(p)
      self.assertEqual(404, st)
      obj = json.loads(body)
      self.assertIn("error", obj)
      self.assertTrue(any("一" <= c <= "鿿" for c in obj["error"]),
              f"错误文案不是中文：{obj['error']}")
      self.assertNotIn("Traceback", body)


if __name__ == "__main__":
  unittest.main(verbosity=2)
