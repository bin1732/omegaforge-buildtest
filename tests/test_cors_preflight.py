"""CORS 预检 + 拒绝后不留脏会话。

两处都是**只在真实 HTTP 层才存在**的问题，且都会被"读代码看着没问题"盖过去：

1) OPTIONS 预检（P0，界面上所有写操作全部失效）
  验证（缺少该约束时，真实 HTTP）：server.py 没有实现 do_OPTIONS，
  BaseHTTPRequestHandler 一律回 501 Unsupported method ('OPTIONS')。
  而前端 api.ts 发 POST 时带 Content-Type: application/json ——
  这不是 CORS 安全listed 类型，浏览器必须先发预检。预检拿不到 2xx，
  真实请求根本不会被发出。两个真实来源全中：
   · Tauri 生产壳：http://tauri.localhost（macOS 为 tauri://localhost）
   · vite 开发态：http://localhost:5173（见 src-tauri/tauri.conf.json）
  即：界面上每一个 POST 都被静默拦下，表现为"点了没反应"。
  该路径不会在真实浏览器之外被触发，因此由本文件覆盖。

2) 超长消息被拒后留下空会话
  _prepare_chat 原本为"先建会话、后校验长度"，于是 400 正确拒绝之后，
  会话列表凭空多出一条，标题是这条超长消息的前 24 字。

回退校验点（撤掉修复，下列用例必须变红）
------------------------------------------
 · 删掉 do_OPTIONS           -> 1/2/3/5 变红（501）
 · 把 _prepare_chat 的长度校验挪回建会话之后 -> 7/8 变红
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

from omegaforge.server import Handler, MSG_MAX_CHARS # noqa: E402


def _start() -> tuple:
  srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
  threading.Thread(target=srv.serve_forever, daemon=True).start()
  return srv, srv.server_address[1]


class _Harness:
  def setUp(self) -> None:
    self._snap = os.environ.get("OMEGAFORGE_HOME")
    os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_ch_")
    self.srv, self.port = _start()
    self.addCleanup(self.srv.shutdown)
    self.addCleanup(self._restore)

  def _restore(self) -> None:
    if self._snap is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = self._snap

  def req(self, method, path, body=None, headers=None, timeout=10):
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

  def preflight(self, origin, path="/api/chat"):
    h = {"Access-Control-Request-Method": "POST",
       "Access-Control-Request-Headers": "content-type"}
    if origin:
      h["Origin"] = origin
    return self.req("OPTIONS", path, headers=h)

  def get_json(self, path):
    st, _h, body = self.req("GET", path)
    try:
      return st, json.loads(body)
    except Exception:
      return st, {}

  def post_json(self, path, payload, origin=None):
    h = {"Content-Type": "application/json"}
    if origin:
      h["Origin"] = origin
    return self.req("POST", path, body=json.dumps(payload), headers=h)


class TestCorsPreflight(_Harness, unittest.TestCase):
  """预检必须成功，否则浏览器不会发出真实请求。"""

  def test_1_tauri_production_shell_preflight_ok(self):
    st, h, _b = self.preflight("http://tauri.localhost")
    self.assertEqual(204, st, f"Tauri 生产壳预检失败：{st}")
    self.assertEqual("http://tauri.localhost",
             h.get("Access-Control-Allow-Origin"))
    self.assertIn("POST", h.get("Access-Control-Allow-Methods") or "")
    self.assertIn("content-type",
           (h.get("Access-Control-Allow-Headers") or "").lower())

  def test_2_vite_dev_origin_preflight_ok(self):
    st, h, _b = self.preflight("http://localhost:5173")
    self.assertEqual(204, st, f"vite 开发态预检失败：{st}")
    self.assertEqual("http://localhost:5173",
             h.get("Access-Control-Allow-Origin"))

  def test_3_untrusted_origin_preflight_rejected(self):
    st, h, _b = self.preflight("https://evil.example")
    self.assertEqual(403, st)
    self.assertIsNone(h.get("Access-Control-Allow-Origin"),
             "恶意来源的预检绝不能给放行头")

  def test_4_non_browser_client_no_origin(self):
    """curl / 我们自己的测试不发 Origin，不应因预检被挡。"""
    st, _h, _b = self.preflight("")
    self.assertEqual(204, st)

  def test_5_preflight_then_real_post_succeeds(self):
    """预检放行之后真实请求必须能发出——端到端串起来才叫通。"""
    p = self.preflight("http://tauri.localhost")[0]
    self.assertEqual(204, p)
    st, _h, _b = self.post_json("/api/chat", {"message": "你好"},
                  origin="http://tauri.localhost")
    self.assertNotEqual(403, st)
    self.assertIn(st, (200, 400), f"真实 POST 异常：{st}")

  def test_6_methods_declared_minimal(self):
    """只声明真正支持的动词，不写 '*'、不含 DELETE/PUT。"""
    _st, h, _b = self.preflight("http://tauri.localhost")
    m = (h.get("Access-Control-Allow-Methods") or "").upper()
    self.assertNotIn("DELETE", m)
    self.assertNotIn("PUT", m)
    self.assertNotIn("*", m)


class TestRejectedRequestLeavesNoTrace(_Harness, unittest.TestCase):
  """被拒绝的请求不能留下用户看得见的脏数据。"""

  def test_7_overlong_message_creates_no_conversation(self):
    _st0, before = self.get_json("/api/conversations")
    n0 = len(before.get("conversations") or [])
    st, _h, body = self.post_json("/api/chat",
                   {"message": "很长" * (MSG_MAX_CHARS + 10)})
    self.assertEqual(400, st, f"超长消息应被 400 拒绝，实际 {st}")
    self.assertIn("过长", body)
    _st1, after = self.get_json("/api/conversations")
    n1 = len(after.get("conversations") or [])
    self.assertEqual(n0, n1,
             f"请求被拒却新建了 {n1 - n0} 个空会话")

  def test_8_overlong_message_stream_path_same(self):
    """流式走同一个 _prepare_chat：不留脏会话，且错误要送达前端。

    注意状态码差异是**不可避免**的：SSE 响应头已发出、状态码改不了，
    所以流式只能回 200 + error 事件（非流式回 400）。这里断言的是
    两件真正要保证的事——错误送达、且不落库。
    """
    _st0, before = self.get_json("/api/conversations")
    n0 = len(before.get("conversations") or [])
    st, _h, body = self.post_json("/api/chat/stream",
                   {"message": "很长" * (MSG_MAX_CHARS + 10)})
    self.assertEqual(200, st)     # SSE 头已发出，改不了状态码
    self.assertIn("过长", body, f"错误未送达前端：{body[:160]}")
    self.assertIn("done", body, "不推 done 前端会永久转圈")
    _st1, after = self.get_json("/api/conversations")
    self.assertEqual(n0, len(after.get("conversations") or []),
             "流式被拒同样不能留下空会话")


if __name__ == "__main__":
  unittest.main(verbosity=2)
