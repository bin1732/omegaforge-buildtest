"""复测：LLM 上游守卫生效后——本地模型仍可用，云元数据/跳转/超大响应被拦。"""
import json
import os
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, "/data/workspace/LATEST")
os.environ["OMEGAFORGE_HOME"] = "/tmp/probe_home_llm2"
os.environ["OMEGAFORGE_ALLOW_KEYLESS"] = "1"

from omegaforge.core.errors import user_error          # noqa: E402
from omegaforge.llm.client import LLMClient            # noqa: E402
from omegaforge.llm.providers import ProviderManager   # noqa: E402

PORT = 18110
SECRET = "SECRET_INTERNAL_METADATA_iam_role=admin"
out = {}


def chat_body():
    return json.dumps({
        "choices": [{"message": {"content": SECRET}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()


class Local(BaseHTTPRequestHandler):
    """模拟本机 Ollama / LM Studio。"""
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
        b = chat_body()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class Redirect(BaseHTTPRequestHandler):
    """公网地址 302 到云元数据。"""
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.send_header("Content-Length", "0")
        self.end_headers()


class Huge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        b = json.dumps({"choices": [{"message": {"content": "x" * (30 * 1024 * 1024)}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srvs = []
for i, h in enumerate((Local, Redirect, Huge)):
    s = HTTPServer(("127.0.0.1", PORT + i), h)
    srvs.append(s)
    threading.Thread(target=s.serve_forever, daemon=True).start()
time.sleep(0.3)

# 1) 本机端点必须仍可用（Ollama / LM Studio 是头号场景）
try:
    r = LLMClient(base_url=f"http://127.0.0.1:{PORT}/v1", api_key="x").chat("s", "u")
    out["1_本机端点仍可用"] = (r.text == SECRET, r.text[:30])
except Exception as e:
    out["1_本机端点仍可用"] = f"EXC {type(e).__name__}: {e}"

try:
    p = ProviderManager.probe(f"http://127.0.0.1:{PORT}/v1", "x")
    out["1b_probe_本机端点"] = (p.get("online") is True, p.get("models"))
except Exception as e:
    out["1b_probe_本机端点"] = f"EXC {type(e).__name__}: {e}"

# 2) 云元数据地址
for host in ("169.254.169.254", "metadata.google.internal", "100.100.100.200"):
    try:
        LLMClient(base_url=f"http://{host}/v1", api_key="x").chat("s", "u")
        out[f"2_{host}"] = "未被拦截（严重）"
    except Exception as e:
        out[f"2_{host}"] = user_error(e, "probe") or f"{type(e).__name__}"

# 3) 跳转到元数据
try:
    r = LLMClient(base_url=f"http://127.0.0.1:{PORT+1}/v1", api_key="x").chat("s", "u")
    out["3_跳转到云元数据"] = f"未被拦截（严重）: {r.text[:40]}"
except Exception as e:
    out["3_跳转到云元数据"] = user_error(e, "probe") or f"{type(e).__name__}"

# 4) 超大响应
tracemalloc.start()
try:
    r = LLMClient(base_url=f"http://127.0.0.1:{PORT+2}/v1", api_key="x").chat("s", "u")
    cur, peak = tracemalloc.get_traced_memory()
    out["4_超大响应"] = f"未封顶：取回 {len(r.text)/1e6:.1f}M 字符，峰值 {peak/1e6:.0f}MB"
except Exception as e:
    cur, peak = tracemalloc.get_traced_memory()
    out["4_超大响应"] = f"{user_error(e, 'probe') or type(e).__name__}；峰值 {peak/1e6:.0f}MB"
tracemalloc.stop()

for s in srvs:
    s.shutdown()
print(json.dumps(out, ensure_ascii=False, indent=2))
