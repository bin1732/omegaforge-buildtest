"""LLM 上游地址守卫：云元数据硬禁、跳转复查、响应封顶、本地端点放行。

回退校验点（见 probes/revert_llm_upstream.py）：
 A 撤掉 client._complete 的接通   → 直连与跳转两处应重犯
 B 撤掉跳转复查           → 公网 302 到元数据应重犯
 C 撤掉响应封顶           → 超大响应应重犯
 D 撤掉本地端点放行（改用公网口径）  → Ollama/LM Studio 应被误杀
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PORT = 18240


class _Local(BaseHTTPRequestHandler):
  def log_message(self, *a):
    pass

  def do_GET(self):
    b = json.dumps({"data": [{"id": "qwen2.5:7b"}]}).encode()
    self.send_response(200)
    self.send_header("Content-Length", str(len(b)))
    self.end_headers()
    self.wfile.write(b)

  def do_POST(self):
    n = int(self.headers.get("Content-Length") or 0)
    self.rfile.read(n)
    b = json.dumps({"choices": [{"message": {"content": "LOCAL_OK"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
    self.send_response(200)
    self.send_header("Content-Length", str(len(b)))
    self.end_headers()
    self.wfile.write(b)


class _Redirect(BaseHTTPRequestHandler):
  def log_message(self, *a):
    pass

  def do_POST(self):
    n = int(self.headers.get("Content-Length") or 0)
    self.rfile.read(n)
    self.send_response(302)
    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
    self.send_header("Content-Length", "0")
    self.end_headers()


class _Huge(BaseHTTPRequestHandler):
  """流式吐 12MB，服务端自身不占内存（否则会污染客户端侧测量）。"""
  def log_message(self, *a):
    pass

  def do_POST(self):
    n = int(self.headers.get("Content-Length") or 0)
    self.rfile.read(n)
    total = 12 * 1024 * 1024
    chunk = b"x" * (64 * 1024)
    self.send_response(200)
    self.send_header("Content-Length", str(total))
    self.end_headers()
    left = total
    try:
      while left > 0:
        w = min(left, len(chunk))
        self.wfile.write(chunk[:w])
        left -= w
    except (BrokenPipeError, ConnectionResetError):
      pass


class UpstreamGuardTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    # 必须原样还原：改了 OMEGAFORGE_HOME / ALLOW_KEYLESS 却不还原，
    # 后面的用例会退出 mock 模式去发真实 HTTP 请求（导致
    # test_arena_fairness 单独跑通过、同批跑 HTTPError）。
    cls._env_before = {k: os.environ.get(k) for k in
              ("OMEGAFORGE_HOME", "OMEGAFORGE_ALLOW_KEYLESS",
              "OMEGAFORGE_BASE_URL", "OMEGAFORGE_API_KEY")}
    os.environ["OMEGAFORGE_HOME"] = "/tmp/test_upstream_guard"
    os.environ["OMEGAFORGE_ALLOW_KEYLESS"] = "1"
    from omegaforge.llm.client import LLMClient
    from omegaforge.llm.providers import ProviderManager
    cls.C, cls.P = LLMClient, ProviderManager
    cls.srvs = []
    for i, h in enumerate((_Local, _Redirect, _Huge)):
      s = HTTPServer(("127.0.0.1", _PORT + i), h)
      cls.srvs.append(s)
      threading.Thread(target=s.serve_forever, daemon=True).start()
    time.sleep(0.3)

  @classmethod
  def tearDownClass(cls):
    for s in cls.srvs:
      s.shutdown()
    for k, v in (cls._env_before or {}).items():
      if v is None:
        os.environ.pop(k, None)
      else:
        os.environ[k] = v

  def test_01_本地端点仍可用(self):
    """防处理过头：Ollama / LM Studio 跑在本机，不能被一起禁掉。"""
    r = self.C(base_url=f"http://127.0.0.1:{_PORT}/v1", api_key="x").chat("s", "u")
    self.assertEqual(r.text, "LOCAL_OK")

  def test_02_probe_本地端点(self):
    p = self.P.probe(f"http://127.0.0.1:{_PORT}/v1", "x")
    self.assertTrue(p.get("online"), p)
    self.assertIn("qwen2.5:7b", p.get("models") or [])

  def test_03_云元数据地址被拒(self):
    for host in ("169.254.169.254", "metadata.google.internal",
           "100.100.100.200", "169.254.170.2"):
      with self.subTest(host=host):
        with self.assertRaises(Exception) as ctx:
          self.C(base_url=f"http://{host}/v1", api_key="x").chat("s", "u")
        msg = str(ctx.exception)
        self.assertIn("云服务内部地址", msg, f"{host} 未被拦: {msg}")

  def test_04_probe_云元数据被拒(self):
    p = self.P.probe("http://169.254.169.254/v1", "")
    self.assertFalse(p.get("online"), p)
    self.assertIn("云服务内部地址", p.get("error", ""), p)

  def test_05_跳转到云元数据被拒(self):
    """公网地址 302 到元数据——只校验初始 URL 会漏掉这一条。"""
    with self.assertRaises(Exception) as ctx:
      self.C(base_url=f"http://127.0.0.1:{_PORT+1}/v1",
          api_key="x").chat("s", "u")
    self.assertIn("云服务内部地址", str(ctx.exception))

  def test_06_超大响应被封顶(self):
    """12MB 响应必须被拒，且不静默截断。"""
    with self.assertRaises(Exception) as ctx:
      self.C(base_url=f"http://127.0.0.1:{_PORT+2}/v1",
          api_key="x").chat("s", "u")
    self.assertIn("过大", str(ctx.exception))

  def test_07_probe_脏模型列表不再整体挂掉(self):
    """上游 /models 返回非字典条目时，只跳过脏项而不是判定离线。"""
    class _Dirty(BaseHTTPRequestHandler):
      def log_message(self, *a):
        pass

      def do_GET(self):
        b = json.dumps({"data": [{"id": "a"}, 123, None,
                     {"id": "b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    s = HTTPServer(("127.0.0.1", _PORT + 9), _Dirty)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
      p = self.P.probe(f"http://127.0.0.1:{_PORT+9}/v1", "x")
      self.assertTrue(p.get("online"), p)
      self.assertEqual(p.get("models"), ["a", "b"])
    finally:
      s.shutdown()

  def test_08_probe_非字典响应不崩(self):
    class _List(BaseHTTPRequestHandler):
      def log_message(self, *a):
        pass

      def do_GET(self):
        b = b"[1,2,3]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    s = HTTPServer(("127.0.0.1", _PORT + 10), _List)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
      p = self.P.probe(f"http://127.0.0.1:{_PORT+10}/v1", "x")
      self.assertFalse(p.get("online"))
      self.assertIn("不是合法", p.get("error", ""), p)
    finally:
      s.shutdown()


if __name__ == "__main__":
  unittest.main(verbosity=2)
