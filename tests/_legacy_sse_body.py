"""server.py SSE 主体（/api/chat/stream 响应头发出之后的整段）端到端守卫。

背景（全部端到端验证：起真实服务 + 假上游，读真实 SSE 字节流）
-------------------------------------------------------------
这一段 87 行缺少该约束时未被真实执行过（mocked 单测走不到），本文件用真实
HTTP 把它跑通，并把两条验证缺陷钉死：

 1. 兜底也失败时只推 error 就 return —— 不推 done。
   SSE 头一旦发出状态码就改不了，前端永远等不到结束信号，loading
   永不结束（用户看着红字一直转圈）。同一段代码里 _prepare_chat
   失败那条路径已经推了 error+done，这个出口漏了。
 2. 兜底成功时上游非流式接口返回了真实 usage，却被丢弃、改用估算并
   标 estimated=True。"流式失败→兜底成功"在无外网时是常态路径，
   于是这一类对话全部按估算记账，用量页系统性失真。

判据说明（避免写出形式化守卫）
----------------------------
断言一律走完整 SSE 通道（真实 HTTP 读字节流、按 "data: " 还原事件），
不直接调内层函数——只测内层会导致"接通断了测试也全绿"。
回退校验点：
 · 1 → 撤回兜底失败分支里的 ev(done)
 · 2 → 把 real_usage 恒改为 None（退回一律估算）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}"
     + (f" — {detail}" if detail else ""))


MODE = {"v": "normal"}


class _Upstream(BaseHTTPRequestHandler):
  """假上游：按 MODE 切换上游形态（正常 / 断流 / 脏包 / 空流 / 502）。"""

  def log_message(self, *a):
    pass

  def _sse(self, chunks: list, close_after: int | None = None):
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.end_headers()
    for i, ch in enumerate(chunks):
      self.wfile.write(ch.encode())
      self.wfile.flush()
      if close_after is not None and i == close_after:
        self.close_connection = True
        try:
          self.wfile.close()
        except Exception:
          pass
        return
    self.wfile.write(b"data: [DONE]\n\n")
    self.wfile.flush()

  def _delta(self, content):
    return "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n\n"

  def do_POST(self):
    n = int(self.headers.get("Content-Length", 0))
    body = json.loads(self.rfile.read(n) or b"{}")
    m = MODE["v"]
    if body.get("stream"):
      if m == "normal":
        self._sse([self._delta("流式"), self._delta("回复")])
      elif m == "midcut":    # 发一半就断，不给 [DONE]
        self._sse([self._delta("流式"), self._delta("回复")], close_after=0)
      elif m == "garbage":    # 不可解析 chunk + 空行 + 注释行
        self._sse(["data: not-json\n\n", "data: \n\n", ": ping\n\n",
              self._delta("好")])
      elif m == "nodelta":    # choices 缺失 / delta 为空
        self._sse(["data: " + json.dumps({"choices": []}) + "\n\n",
              "data: " + json.dumps({"choices": [{"delta": {}}]}) + "\n\n"])
      elif m == "dictcontent":  # delta.content 是非字符串
        self._sse([self._delta({"a": 1})])
      else:
        self._sse([])
    else:
      # 非流式兜底
      if m in ("502", "midcut", "dictcontent"):
        raw = json.dumps({"error": "upstream down",
                 "code": "E_UPSTREAM"}).encode()
        self.send_response(500)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return
      raw = json.dumps({"choices": [{"message": {"content": "流式回复"}}],
               "model": "m",
               "usage": {"prompt_tokens": 100,
                    "completion_tokens": 20}}).encode()
      self.send_response(200)
      self.send_header("Content-Length", str(len(raw)))
      self.end_headers()
      self.wfile.write(raw)

  def do_GET(self):
    raw = json.dumps({"data": [{"id": "m"}]}).encode()
    self.send_response(200)
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)


HOME = tempfile.mkdtemp(prefix="ssebody_")
os.makedirs(os.path.join(HOME, "skills", "coder"), exist_ok=True)
with open(os.path.join(HOME, "skills", "coder", "SKILL.md"), "w", encoding="utf-8") as _f:
  _f.write("---\nname: coder\ndescription: d\n---\n你是一名资深工程师。\n")

_up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
_UPP = _up.server_address[1]
threading.Thread(target=_up.serve_forever, daemon=True).start()

with open(os.path.join(HOME, "providers.json"), "w", encoding="utf-8") as _f:
  json.dump({"active": "custom",
        "configs": {"custom": {"base_url": f"http://127.0.0.1:{_UPP}/v1",
                   "api_key": "k",
                   "models": {"main": "m", "fast": "m"}}}}, _f)

# 必须在 import server 之前设置：模块级单例会在此刻绑定数据目录
_ENV_HOME_BEFORE = os.environ.get("OMEGAFORGE_HOME")
os.environ["OMEGAFORGE_HOME"] = HOME


def teardown_module(module):
  """还原 OMEGAFORGE_HOME。

  模块级赋值在 import（收集阶段）就生效，会泄漏到**之后所有模块**：
  验证 test_cross_version_compare 单独跑 13 项全过，与本模块同批跑
  则 3 项失败。不还原的话"全绿"取决于执行顺序。
  """
  if _ENV_HOME_BEFORE is None:
    os.environ.pop("OMEGAFORGE_HOME", None)
  else:
    os.environ["OMEGAFORGE_HOME"] = _ENV_HOME_BEFORE


import omegaforge.server as S # noqa: E402

_srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
_SP = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def sse_raw(payload: dict) -> str:
  req = urllib.request.Request(
    f"http://127.0.0.1:{_SP}/api/chat/stream",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"})
  return urllib.request.urlopen(req, timeout=30).read().decode()


def events(body: str) -> list:
  out = []
  for line in body.split("\n"):
    if line.startswith("data: "):
      try:
        out.append(json.loads(line[6:]))
      except Exception:
        out.append({"__bad__": line[6:][:80]})
  return out


def types_(body: str) -> list:
  return [e.get("type") for e in events(body) if "type" in e]


def text_of(body: str) -> str:
  return "".join(e.get("text", "")
          for e in events(body) if e.get("type") == "delta")


def main() -> int:
  print("\n=== SSE 主体端到端 ===")

  # A. 正常流
  MODE["v"] = "normal"
  b = sse_raw({"message": "一句话"})
  check("1 正常流事件序列 start/delta*/done",
     types_(b) == ["start", "delta", "delta", "done"], str(types_(b)))
  blob = [ln for ln in b.split("\n") if "HTTP/" in ln or "Content-Type:" in ln]
  check("2 流内不夹带第二个 HTTP 报文", not blob, str(blob[:1]))

  # B. 中途断流
  MODE["v"] = "midcut"
  b = sse_raw({"message": "一句话"})
  check("3 中途断流仍以 done 收尾", types_(b)[-1] == "done", str(types_(b)))
  check("4 中途断流正文不重复", text_of(b).count("流式") <= 1, f"正文={text_of(b)!r}")

  # C. 上游 502 + 兜底也失败（本文件要钉死的核心两条）
  MODE["v"] = "502"
  b = sse_raw({"message": "一句话"})
  ev = events(b)
  check("5 兜底也失败必须推 done（否则前端永久转圈）",
     "done" in [e.get("type") for e in ev], str(types_(b)))
  check("6 兜底也失败仍推 error", "error" in [e.get("type") for e in ev], str(types_(b)))
  _dn = [e for e in ev if e.get("type") == "done"]
  check("7 失败收尾带 finish_reason=error",
     bool(_dn) and _dn[0].get("finish_reason") == "error", str(_dn[:1]))

  # D. 脏 chunk 容错
  MODE["v"] = "garbage"
  b = sse_raw({"message": "一句话"})
  check("8 脏 chunk 后仍拿到正文", text_of(b) == "好", f"正文={text_of(b)!r}")

  # E. 空流 → 走兜底
  MODE["v"] = "nodelta"
  b = sse_raw({"message": "一句话"})
  check("9 空流走兜底拿到正文", text_of(b) == "流式回复",
     f"正文={text_of(b)!r} types={types_(b)}")
  _dn = [e for e in events(b) if e.get("type") == "done"]
  check("10 兜底成功用真实用量（estimated=False 且 120）",
     bool(_dn) and _dn[0].get("estimated") is False and _dn[0].get("tokens") == 120,
     str(_dn[:1]))

  # F. delta.content 非字符串
  MODE["v"] = "dictcontent"
  b = sse_raw({"message": "一句话"})
  check("11 非字符串 delta 不产生重复正文且收尾 done",
     text_of(b).count("流式") <= 1 and types_(b)[-1] == "done",
     f"正文={text_of(b)!r} types={types_(b)}")

  # G. 纯流式仍标估算（防止第 10 项改动过头）
  MODE["v"] = "normal"
  b = sse_raw({"message": "一句话"})
  _dn = [e for e in events(b) if e.get("type") == "done"]
  check("12 纯流式仍标 estimated=True（未处理过头）",
     bool(_dn) and _dn[0].get("estimated") is True, str(_dn[:1]))

  # H. 准备阶段失败（陈旧会话ID）：头已发出，必须 error+done 收尾
  MODE["v"] = "normal"
  b = sse_raw({"message": "一句话", "conversation_id": "conv-does-not-exist"})
  check("13 准备阶段失败也必须以 done 收尾",
     types_(b)[-1] == "done" and "error" in types_(b), str(types_(b)))

  # I. 空消息：头尚未发出，必须是 400 JSON 而不是 SSE
  req = urllib.request.Request(
    f"http://127.0.0.1:{_SP}/api/chat/stream",
    data=json.dumps({"message": "  "}).encode(),
    headers={"Content-Type": "application/json"})
  try:
    urllib.request.urlopen(req, timeout=30)
    ok_empty, det_empty = False, "未抛 HTTPError"
  except urllib.error.HTTPError as he:
    body = he.read().decode()
    ok_empty = he.code == 400 and "data:" not in body
    det_empty = f"code={he.code} body={body[:60]}"
  check("14 空消息返回 400 JSON（不误发 SSE 头）", ok_empty, det_empty)

  TOTAL = len(PASS) + len(FAIL)
  print(f"\n通过 {len(PASS)}/{TOTAL}")
  if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
  


if __name__ == "__main__":
  raise SystemExit(main())
