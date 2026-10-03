"""server.py 覆盖率检查脚本：在 coverage 进程内起真实服务并驱动请求。

为什么单独写：既有套件走子进程（test_legacy_suites），server.py 因此在
coverage 进程里没有被 import，"覆盖率"量不到。本文件在同一进程内起
ThreadingHTTPServer，用真实 HTTP 把路由层跑一遍，输出未覆盖行区间。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        self.rfile  # noqa: B018
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for t in ("你", "好"):
                self.wfile.write(
                    ("data: " + json.dumps(
                        {"choices": [{"delta": {"content": t}}]}) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        raw = json.dumps({"choices": [{"message": {"content": "ok"}}],
                          "model": "m",
                          "usage": {"prompt_tokens": 10,
                                    "completion_tokens": 5}}).encode()
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


HOME = tempfile.mkdtemp(prefix="covsrv_")
_up = ThreadingHTTPServer(("127.0.0.1", 0), _Up)
_UPP = _up.server_address[1]
threading.Thread(target=_up.serve_forever, daemon=True).start()

os.makedirs(os.path.join(HOME, "skills", "coder"), exist_ok=True)
with open(os.path.join(HOME, "skills", "coder", "SKILL.md"), "w", encoding="utf-8") as f:
    f.write("---\nname: coder\ndescription: d\n---\n正文\n")

with open(os.path.join(HOME, "providers.json"), "w", encoding="utf-8") as f:
    json.dump({"active": "custom",
               "configs": {"custom": {
                   "base_url": f"http://127.0.0.1:{_UPP}/v1",
                   "api_key": "k",
                   "models": {"main": "m", "fast": "m", "judge": "m"}}}}, f)

os.environ["OMEGAFORGE_HOME"] = HOME

import omegaforge.server as S  # noqa: E402

_srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
SP = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = f"http://127.0.0.1:{SP}"


def get(path, headers=None, timeout=20):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def post(path, obj, headers=None, timeout=30):
    data = json.dumps(obj).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    seen = []
    gets = [
        "/api/status", "/api/usage", "/api/tools/permissions", "/api/tools/policy",
        "/api/tools/audit", "/api/providers", "/api/providers/models",
        "/api/personas", "/api/conversations", "/api/kb/list", "/api/wiki/list",
        "/api/tasks/list", "/api/skills/list", "/api/runs", "/api/memory/recall?q=x",
        "/api/kb/search?q=x", "/api/wiki/page?slug=nope",
    ]
    for p in gets:
        st, _b = get(p)
        seen.append(("GET", p, st))

    # 会话：新建 -> 列 -> 改名/改模型 -> 删
    st, b = post("/api/conversations/new", {"title": "t"})
    seen.append(("POST", "/api/conversations/new", st))
    cid = ""
    try:
        cid = json.loads(b).get("id", "")
    except Exception:
        pass

    st, b = post("/api/chat", {"message": "你好", "conversation_id": cid})
    seen.append(("POST", "/api/chat", st))
    st, _b = post("/api/chat/stream", {"message": "你好", "conversation_id": cid})
    seen.append(("POST", "/api/chat/stream", st))

    if cid:
        seen.append(("POST", "/api/conversations/model",
                     post("/api/conversations/model", {"id": cid, "model": "m"})[0]))
        seen.append(("POST", "/api/conversations/delete",
                     post("/api/conversations/delete", {"id": cid})[0]))

    posts = [
        ("/api/kb/add", {"title": "T", "text": "正文"}),
        ("/api/memory/remember", {"text": "记住"}),
        ("/api/memory/recall", {"q": "记住"}),
        ("/api/wiki/save", {"slug": "s", "title": "S", "body": "b"}),
        ("/api/tasks/add", {"text": "任务", "priority": 2}),
        ("/api/tasks/done", {"id": "nope"}),
        ("/api/skills/invoke", {"name": "coder", "args": ""}),
        ("/api/skills/install", {"source": ""}),
        ("/api/tools/exec", {"tool": "fs.list", "args": {"path": "."}}),
        ("/api/tools/permissions", {"fs.read": True}),
        ("/api/tools/policy", {"mode": "confirm"}),
        ("/api/providers/apply", {"name": "custom", "base_url": f"http://127.0.0.1:{_UPP}/v1",
                                  "api_key": "k", "models": {"main": "m"}}),
        ("/api/providers/test", {"name": "custom",
                                 "base_url": f"http://127.0.0.1:{_UPP}/v1",
                                 "models": {"main": "m"}}),
        ("/api/voice/status", {}),
        ("/api/voice/tts", {"text": "你好", "speed": 1.0}),
        ("/api/voice/asr", {"path": "/nonexistent.wav"}),
        ("/api/distill", {"source": "你是一个助手，请帮我做事。" * 12}),
    ]
    for p, o in posts:
        st, _b = post(p, o)
        seen.append(("POST", p, st))

    bad = [(m, p, s) for m, p, s in seen if s in (500, -1)]
    print("\n=== 驱动结果 ===")
    for m, p, s in seen:
        print(f"  {s:>4}  {m} {p}")
    print(f"\n500/-1 数量: {len(bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
