"""探测：模型接口地址（base_url）是否存在 SSRF + 响应回显 + 无体积上限。

不依赖 pytest，直接起假内网服务走真实调用路径。
"""
import json
import os
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, "/data/workspace/LATEST")
os.environ["OMEGAFORGE_HOME"] = "/tmp/probe_home_llm"
os.environ["OMEGAFORGE_ALLOW_KEYLESS"] = "1"

PORT = 18099
SECRET = "SECRET_INTERNAL_METADATA_iam_role=admin"


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/models"):
            body = json.dumps({"data": [{"id": "internal-secret-model"}]})
        else:
            body = json.dumps({"secret": SECRET})
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({
            "choices": [{"message": {"content": SECRET}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = HTTPServer(("127.0.0.1", PORT), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

from omegaforge.llm.client import LLMClient          # noqa: E402
from omegaforge.llm.providers import ProviderManager  # noqa: E402

results = {}

# ---- A: 直接调用，base_url 指向本机内部服务 ----
try:
    c = LLMClient(base_url=f"http://127.0.0.1:{PORT}/v1", api_key="x")
    r = c.chat("sys", "user")
    results["A_直连内部地址_取回内容"] = (SECRET in r.text, r.text[:60])
except Exception as e:
    results["A_直连内部地址_取回内容"] = ("EXC", f"{type(e).__name__}: {e}")

# ---- B: probe()（/api/providers/test 后端）----
try:
    p = ProviderManager.probe(f"http://127.0.0.1:{PORT}/v1", "x")
    leaked = SECRET in json.dumps(p, ensure_ascii=False) or \
        "internal-secret-model" in json.dumps(p)
    results["B_probe_内部地址"] = (leaked, p)
except Exception as e:
    results["B_probe_内部地址"] = ("EXC", f"{type(e).__name__}: {e}")

# ---- C: 云元数据地址是否被判为不可达（有守卫则应被拒）----
tried = []
for host in ("169.254.169.254", "metadata.google.internal"):
    try:
        ProviderManager.probe(f"http://{host}/v1", "")
        tried.append((host, "未被拦截（发起了请求）"))
    except Exception as e:
        tried.append((host, f"被拦: {type(e).__name__}"))
    except BaseException:
        tried.append((host, "???"))
results["C_云元数据地址"] = tried

# ---- D: 响应体无上限 ----
class Big(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        payload = "x" * (30 * 1024 * 1024)
        body = json.dumps({
            "choices": [{"message": {"content": payload}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv2 = HTTPServer(("127.0.0.1", PORT + 1), Big)
threading.Thread(target=srv2.serve_forever, daemon=True).start()
time.sleep(0.3)
tracemalloc.start()
try:
    c2 = LLMClient(base_url=f"http://127.0.0.1:{PORT+1}/v1", api_key="x")
    r2 = c2.chat("s", "u")
    cur, peak = tracemalloc.get_traced_memory()
    results["D_响应体无上限"] = (f"取回 {len(r2.text)/1e6:.1f}M 字符, "
                                 f"峰值内存 {peak/1e6:.0f}MB")
except Exception as e:
    results["D_响应体无上限"] = f"EXC {type(e).__name__}: {e}"
tracemalloc.stop()

srv.shutdown()
srv2.shutdown()

print(json.dumps(results, ensure_ascii=False, indent=2))
