"""全接口入参异常探测：真起服务，逐条发畸形入参，记录真实返回。

为什么做这个
------------
用户填错格式时最该得到的是"哪一项填错了、应该怎么填"，而不是
"服务器故障"。后者会让用户反复重试同一件事，也掩盖了真正的入参缺陷。

本脚本不推断、不读代码下结论：每个端点真发请求，记录真实状态码与
真实文案，再按两条判据分类：
    1) 是否落到了「操作失败，请稍后重试」这类无法行动的兜底
    2) 是否把内部名（英文字段名、路径、异常类名）带进了用户可见文案

用法：python probes/probe_endpoint_arg_errors.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = "/data/workspace/LATEST"
sys.path.insert(0, ROOT)

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

# 兜底文案：出现即代表用户拿不到可行动的信息
GENERIC = ["操作失败，请稍后重试", "服务器故障", "内部错误", "请求内容有误，请检查后重试"]
# 内部名特征：不应出现在用户可见文案里
INTERNAL_HINTS = ["TaskStore", "UsageStore", "traceback", ".omegaforge",
                  "Exception", "Error:", "/data/", "/home/", "/tmp/"]


def post(path: str, payload) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


CASES = [
    ("/api/distill", [{}]),
    ("/api/chat", [
        {},
        {"message": 123},
        {"message": "hi", "conversation_id": 123},
    ]),
    ("/api/conversations/new", [{"title": 123}]),
    ("/api/conversations/delete", [{}, {"id": 123}]),
    ("/api/conversations/model", [{}, {"id": "x", "model": 123}]),
    ("/api/voice/tts", [{}, {"text": 123}, {"text": "hi", "speed": "fast"}]),
    ("/api/tools/permissions", [{"x": 1}]),
    ("/api/tools/policy", [{"mode": "weird"}, {"mode": 123}]),
    ("/api/tools/exec", [{}, {"tool": 123}, {"tool": "fs_read", "args": "x"}]),
    ("/api/kb/search", [{}, {"q": 123}]),
    ("/api/memory/recall", [{}, {"q": 123}]),
    ("/api/kb/add", [{}, {"title": 123}, {"title": "t", "body": 123}]),
    ("/api/wiki/save", [{}, {"title": 123}]),
    ("/api/tasks/add", [{}, {"text": 123}, {"text": "t", "priority": "high"}]),
    ("/api/tasks/done", [{}, {"task_id": 123}]),
    ("/api/skills/install", [{}, {"path": 123}]),
    ("/api/skills/invoke", [{}, {"name": 123}]),
    ("/api/memory/remember", [{}, {"text": 123}]),
    ("/api/providers/apply", [{}, {"name": 123}, {"name": "openai", "models": "x"}]),
    ("/api/providers/test", [{}, {"name": 123}]),
]


def main() -> int:
    home = "/tmp/argerr_home"
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = home
    os.environ["OMEGAFORGE_MODE"] = "auto_edit"

    import omegaforge.server as srv
    srv.RUNS.home = None
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    generic_hits = []
    internal_hits = []
    rows = []
    try:
        for path, payloads in CASES:
            for i, payload in enumerate(payloads):
                try:
                    code, raw = post(path, payload)
                except Exception as e:
                    rows.append((path, i, "EXC", str(e)[:80]))
                    continue
                try:
                    msg = json.loads(raw).get("error") or json.loads(raw).get("message") or ""
                except Exception:
                    msg = raw[:120]
                msg = str(msg)
                rows.append((path, i, code, msg))
                if any(g in msg for g in GENERIC):
                    generic_hits.append((path, i, payload, code, msg))
                for h in INTERNAL_HINTS:
                    if h in msg:
                        internal_hits.append((path, i, payload, code, msg, h))
                        break
    finally:
        httpd.shutdown()

    print("=== 全部返回 ===")
    for path, i, code, msg in rows:
        print(f"  {path} #{i} -> {code}  {msg[:90]}")

    print("\n=== 落在无法行动的兜底 ===")
    if not generic_hits:
        print("  无")
    for path, i, payload, code, msg in generic_hits:
        print(f"  {path} #{i} payload={payload} -> {code} {msg[:90]}")

    print("\n=== 文案带内部名 ===")
    if not internal_hits:
        print("  无")
    for path, i, payload, code, msg, h in internal_hits:
        print(f"  {path} #{i} payload={payload} hint={h} -> {code} {msg[:90]}")

    print(f"\n总计 {len(rows)} 条；兜底 {len(generic_hits)}；内部名 {len(internal_hits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
