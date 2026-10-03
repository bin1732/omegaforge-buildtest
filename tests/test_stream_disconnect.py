"""流式对话中途断开时的落库口径守卫。

桌面应用的真实场景：回复还在逐字推送，用户关掉窗口或切走页面，连接
断开。这类"用户主动离开"既不是服务端故障，也不该让界面上的文字凭空
消失。要钉住四件事：

 1. 已推送出去的内容必须落库，并标记为被中断 —— 否则界面上它是一条
   样式完好的普通回答，用户会当作完整答案，后续提问还基于这半句话
   继续往下接。
 2. 客户端断开**不得**记进错误日志 —— 否则真实故障会被这类噪声淹没。
 3. 断开后服务端仍要能正常响应，不能被一个坏连接拖垮。
 4. 防处理过头：正常跑完的回复**不能**带中断标记。

全部走真实 HTTP + 真实 SSE 上游（socket 自建），不用替身：流式端点
用的是裸 urlopen，不走 LLMClient，因此 MOCK=1 对它不生效（不配上游
地址它会真的去连 api.openai.com）。上游地址只能通过 /api/providers/apply
写入，和用户在界面上配置供应商是同一条路径。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
  sys.path.insert(0, REPO)

CHUNKS = ["你", "好", "，", "这", "是", "一", "段", "完", "整", "回", "复"]
FULL = "".join(CHUNKS)


class _SseUpstream:
  """真的 SSE 上游：推 chunk，推够 hold 个就收尾（不发 [DONE]）。"""

  def __init__(self, hold: int | None = None):
    self.hold = hold
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.bind(("127.0.0.1", 0))
    self.port = self.sock.getsockname()[1]
    self.sock.listen(8)
    self.stop = False
    threading.Thread(target=self._accept, daemon=True).start()

  def _accept(self):
    while not self.stop:
      try:
        c, _ = self.sock.accept()
      except OSError:
        return
      threading.Thread(target=self._serve, args=(c,), daemon=True).start()

  def _serve(self, c):
    try:
      buf = b""
      while b"\r\n\r\n" not in buf:
        d = c.recv(4096)
        if not d:
          return
        buf += d
      c.sendall(b"HTTP/1.1 200 OK\r\n"
           b"Content-Type: text/event-stream\r\n\r\n")
      for i, ch in enumerate(CHUNKS):
        try:
          c.sendall(("data: " + json.dumps(
            {"choices": [{"delta": {"content": ch}}]},
            ensure_ascii=False) + "\n\n").encode())
        except OSError:
          return     # 客户端已断开
        if self.hold is not None and i + 1 >= self.hold:
          return     # 上游收尾，而客户端早已不在
        time.sleep(0.05)
      c.sendall(b"data: [DONE]\n\n")
    except Exception:
      pass
    finally:
      try:
        c.close()
      except Exception:
        pass


class _Case(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.home = tempfile.mkdtemp(prefix="stream_disc_")
    cls.up = _SseUpstream(hold=6)

    saved = dict(os.environ)
    cls._saved = saved
    os.environ["OMEGAFORGE_HOME"] = cls.home
    os.environ["OMEGAFORGE_MOCK"] = "1"

    import omegaforge.server as srv
    cls.srv = srv
    for s in (srv.KB, srv.WIKI, srv.TASKS, srv.USAGE,
         srv.PROVIDERS, srv.CONVS, srv.RUNS):
      inv = getattr(s, "invalidate", None)
      if inv is not None:
        inv()

    cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
    threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    cls._post("/api/providers/apply", {
      "name": "custom",
      "base_url": f"http://127.0.0.1:{cls.up.port}/v1",
      "api_key": "probe-key",
      "models": {"main": "probe-main", "fast": "probe-fast",
            "judge": "probe-judge"}})

  @classmethod
  def tearDownClass(cls):
    cls.httpd.shutdown()
    cls.up.stop = True
    for k, v in cls._saved.items():
      os.environ[k] = v
    for k in list(os.environ):
      if k not in cls._saved:
        os.environ.pop(k, None)
    shutil.rmtree(cls.home, ignore_errors=True)

  # ------------------------------------------------------------ 工具 --

  @classmethod
  def _post(cls, path, payload):
    req = urllib.request.Request(
      cls.base + path, data=json.dumps(payload).encode(),
      headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=30)

  @classmethod
  def _get(cls, path):
    return json.loads(urllib.request.urlopen(
      cls.base + path, timeout=10).read())

  def _stream(self, read_events: int):
    """发起流式请求，读够 n 个事件后断开，返回会话 id。"""
    cid = None
    with self._post("/api/chat/stream", {"message": "你好"}) as r:
      n = 0
      try:
        for raw in r:
          line = raw.decode("utf-8", "replace").strip()
          if not line.startswith("data:"):
            continue
          obj = json.loads(line[5:].strip())
          if obj.get("type") == "start":
            cid = obj.get("conversation_id")
          n += 1
          if n >= read_events:
            break
      finally:
        r.close()      # 客户端断开
    time.sleep(1.5)       # 给服务端收尾
    return cid

  def _messages(self, cid):
    """取会话消息。列表接口只给 id/title/count，不含消息全文。"""
    try:
      d = self._get(f"/api/conversations/{cid}")
      msgs = (d.get("conversation") or d or {}).get("messages")
      if msgs:
        return msgs
    except Exception:                  # noqa: BLE001
      pass
    fp = os.path.join(self.home, "conversations", f"{cid}.json")
    self.assertTrue(os.path.exists(fp), f"会话文件不存在: {fp}")
    return json.load(open(fp, encoding="utf-8")).get("messages") or []

  def _log_text(self):
    out = []
    logd = os.path.join(self.home, "logs")
    if os.path.isdir(logd):
      for fn in os.listdir(logd):
        fp = os.path.join(logd, fn)
        if os.path.isfile(fp):
          out.append(open(fp, encoding="utf-8",
                  errors="replace").read())
    return "\n".join(out)

  # ------------------------------------------------------------ 守卫 --

  def test_01_中断后仍落库且带标记(self):
    cid = self._stream(read_events=3)
    self.assertIsNotNone(cid, "未取到会话 id")
    assistant = [m for m in self._messages(cid)
           if m.get("role") == "assistant"]
    self.assertTrue(assistant, "断开后助手回复凭空消失：界面上显示过的"
                  "文字应当保留")
    saved = "".join(m.get("content", "") for m in assistant)
    self.assertTrue(FULL.startswith(saved) and saved,
            f"落库内容不是已推送的前缀: {saved!r}")
    # 半截本身可接受，**没标出来**不可接受。
    if saved != FULL:
      self.assertTrue(any(m.get("interrupted") for m in assistant),
              f"半截回复未标记被中断，界面上会以完整回答的"
              f"样式呈现: {saved!r}")

  def test_02_客户端断开不记进错误日志(self):
    self._stream(read_events=3)
    text = self._log_text()
    for kw in ("BrokenPipe", "ConnectionReset", "Broken pipe"):
      self.assertNotIn(kw, text,
               f"客户端断开被当成故障记进日志（{kw}），"
               f"真实故障会被这类噪声淹没")

  def test_03_断开后服务端仍可响应(self):
    self._stream(read_events=3)
    st = self._get("/api/status")
    self.assertTrue(st.get("version"), "断开后服务端无响应")

  def test_04_正常跑完不带中断标记(self):
    """防处理过头：完整送达的回复不能被标成"被中断"。"""
    # 前三条用例的上游只推 6 个 chunk 就收尾（为了造中断）；这条要
    # 完整的回复，临时放开上限，跑完恢复，避免影响别的用例。
    self.up.hold = None
    try:
      self._run_full_case()
    finally:
      self.up.hold = 6

  def _run_full_case(self):
    cid = None
    with self._post("/api/chat/stream", {"message": "完整回复"}) as r:
      for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
          continue
        obj = json.loads(line[5:].strip())
        if obj.get("type") == "start":
          cid = obj.get("conversation_id")
        if obj.get("type") == "done":
          break
    time.sleep(1.5)
    self.assertIsNotNone(cid)
    assistant = [m for m in self._messages(cid)
           if m.get("role") == "assistant"]
    self.assertTrue(assistant, "完整流式回复未落库")
    saved = "".join(m.get("content", "") for m in assistant)
    self.assertEqual(saved, FULL, f"完整回复内容不对: {saved!r}")
    self.assertFalse(any(m.get("interrupted") for m in assistant),
             "正常完成的回复被误标为被中断")


if __name__ == "__main__":
  unittest.main(verbosity=2)
