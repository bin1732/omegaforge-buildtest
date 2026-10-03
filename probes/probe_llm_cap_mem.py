"""只测客户端侧：假服务流式吐字节（自身不占内存），对比封顶前后峰值。

上一版把 30MB 字符串拼在同一进程里，测到的 94MB 是探测脚本自己的开销，
不是客户端的——必须隔离再测。
"""
import json
import os
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, "/data/workspace/LATEST")
os.environ["OMEGAFORGE_HOME"] = "/tmp/probe_home_llm3"
os.environ["OMEGAFORGE_ALLOW_KEYLESS"] = "1"

from omegaforge.llm import upstream_guard                 # noqa: E402
from omegaforge.llm.client import LLMClient               # noqa: E402

TOTAL = 60 * 1024 * 1024
CHUNK = b"x" * (64 * 1024)


class Stream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Length", str(TOTAL))
        self.end_headers()
        left = TOTAL
        while left > 0:
            w = min(left, len(CHUNK))
            self.wfile.write(CHUNK[:w])
            left -= w


srv = HTTPServer(("127.0.0.1", 18120), Stream)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

out = {}

# 封顶生效时
tracemalloc.start()
try:
    LLMClient(base_url="http://127.0.0.1:18120/v1", api_key="x").chat("s", "u")
    out["封顶后"] = "未被拦截（异常）"
except Exception:
    pass
_, peak = tracemalloc.get_traced_memory()
out["封顶后_客户端峰值MB"] = round(peak / 1e6, 1)
tracemalloc.stop()

# 撤掉封顶（把上限提到 100MB，等价于改动前的行为）
upstream_guard.MAX_UPSTREAM_BYTES = 100 * 1024 * 1024
import importlib
importlib.reload(upstream_guard)
upstream_guard.MAX_UPSTREAM_BYTES = 100 * 1024 * 1024
import omegaforge.llm.client as C
C.open_upstream = upstream_guard.open_upstream
tracemalloc.start()
try:
    r = C.LLMClient(base_url="http://127.0.0.1:18120/v1", api_key="x").chat("s", "u")
    got = len(r.text)
except Exception as e:  # noqa: F841 —— 保留异常对象供下方输出类型名
    got = f"EXC {type(e).__name__}"
_, peak2 = tracemalloc.get_traced_memory()
tracemalloc.stop()
out["未封顶_客户端峰值MB"] = round(peak2 / 1e6, 1)
out["未封顶_取回字符数"] = got

srv.shutdown()
print(json.dumps(out, ensure_ascii=False, indent=2))
