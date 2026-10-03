"""对真实服务端验证语音模型下载端点（进程内起服务，不用后台进程）。"""
import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

home = tempfile.mkdtemp(prefix="vhome-")
os.environ["OMEGAFORGE_HOME"] = home

from omegaforge.server import Handler  # noqa: E402

httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Origin": "http://127.0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(path):
    req = urllib.request.Request(
        base + path, headers={"Origin": "http://127.0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        fails.append(name)


c, b = post("/api/voice/models/progress", {})
check("缺 kind 返回 400 且给中文提示",
      c == 400 and "模型类型" in str(b.get("error")), f"{c} {b}")

c, b = post("/api/voice/models/progress", {"kind": "xx"})
check("非法 kind 返回 400", c == 400 and "模型类型" in str(b.get("error")), f"{c} {b}")

c, b = get("/api/voice/models/progress?kind=asr")
check("GET 查询参数回退生效（前端轮询用 GET）",
      c == 200 and b.get("state") in ("unknown", "idle", "done"), f"{c} {b}")

c, b = post("/api/voice/models/install", {"kind": "xx"})
check("install 非法 kind 返回 400", c == 400, f"{c} {b}")

c, b = get("/api/voice/status")
check("语音状态端点仍正常", c == 200 and "asr" in b and "tts" in b, f"{c}")

httpd.shutdown()
print()
print("=== 结果 ===")
print(f"失败 {len(fails)} 项" + ("" if not fails else "：" + "、".join(fails)))
raise SystemExit(1 if fails else 0)
