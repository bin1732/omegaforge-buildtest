"""探测：CORS 预检（OPTIONS）是否被实现。

Tauri 生产壳的前端来自 http://tauri.localhost（Win/Linux）或
tauri://localhost（macOS），向 127.0.0.1:8787 发 POST 且带
Content-Type: application/json —— 这不是 CORS 安全listed 类型，
浏览器必须先发 OPTIONS 预检。若服务端不实现 do_OPTIONS，
BaseHTTPRequestHandler 返回 501，预检失败 → 所有写操作被浏览器拦下。
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HOME = tempfile.mkdtemp(prefix="opts_")
os.environ["OMEGAFORGE_HOME"] = HOME

import omegaforge.server as S  # noqa: E402

_srv = __import__("http.server", fromlist=["x"]).ThreadingHTTPServer(
    ("127.0.0.1", 0), S.Handler)
SP = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = f"http://127.0.0.1:{SP}"


def options(path, origin, method="POST", headers="content-type"):
    req = urllib.request.Request(BASE + path, method="OPTIONS")
    if origin:
        req.add_header("Origin", origin)
    req.add_header("Access-Control-Request-Method", method)
    req.add_header("Access-Control-Request-Headers", headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except Exception as e:
        return -1, {"err": f"{type(e).__name__}: {e}"}


def main() -> int:
    print("\n=== CORS 预检（OPTIONS）实测 ===")
    for origin in ("http://tauri.localhost", "http://localhost:5173",
                   "https://evil.example", ""):
        st, h = options("/api/chat", origin)
        allow = h.get("Access-Control-Allow-Origin", "-")
        meth = h.get("Access-Control-Allow-Methods", "-")
        hdrs = h.get("Access-Control-Allow-Headers", "-")
        print(f"  Origin={origin!r:34} -> {st}  ACAO={allow!r} "
              f"ACAM={meth!r} ACAH={hdrs!r}")

    print("\n=== 对照：真实的 POST 是否被拦（同源/无 Origin） ===")
    data = json.dumps({"message": "你好"}).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  POST /api/chat (无 Origin) -> {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  POST /api/chat (无 Origin) -> {e.code}")
    except Exception as e:
        print(f"  POST /api/chat (无 Origin) -> {type(e).__name__}: {e}")

    print("\n=== 对照：恶意来源 POST 是否被 403 ===")
    req = urllib.request.Request(BASE + "/api/chat", data=data,
                                 headers={"Content-Type": "application/json",
                                          "Origin": "https://evil.example"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  -> {r.status}  (不应出现：恶意来源应被 403)")
    except urllib.error.HTTPError as e:
        print(f"  -> {e.code} {e.read().decode()[:80]}")
    except Exception as e:
        print(f"  -> {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
