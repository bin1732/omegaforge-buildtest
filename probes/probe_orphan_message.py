"""探测：超长消息被拒后，会话里是否留下孤立的 user 消息。

_prepare_chat 的顺序是：
    cid 解析 -> 建会话 -> add_message(user) -> ... -> 预算复核 -> raise
即"先写入，后校验"。一旦复核失败（消息过长 / 人设正文过长），
用户拿到 400，但那条 user 消息已经落库且没有 assistant 回复。
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

HOME = tempfile.mkdtemp(prefix="orphan_")
os.environ["OMEGAFORGE_HOME"] = HOME

import omegaforge.server as S  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

_srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
SP = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = f"http://127.0.0.1:{SP}"


def post(path, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(path):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    print("\n=== 超长消息被拒后的会话残留 ===")
    before = json.loads(get("/api/conversations")[1])["conversations"]
    n0 = len(before)
    print(f"  请求前会话数: {n0}")

    st, body = post("/api/chat", {"message": "很长" * 100000})
    print(f"  超长消息 -> HTTP {st}: {body[:120]}")

    after = json.loads(get("/api/conversations")[1])["conversations"]
    print(f"  请求后会话数: {len(after)}  (新增 {len(after) - n0})")

    orphan = 0
    for c in after:
        msgs = c.get("messages") or []
        if msgs and msgs[-1].get("role") == "user" and len(msgs) == 1:
            orphan += 1
    print(f"  孤立会话（只有一条 user 消息、没有任何回复）: {orphan}")
    for c in after:
        msgs = c.get("messages") or []
        if msgs and len(msgs) == 1 and msgs[-1].get("role") == "user":
            print(f"    · id={c.get('id')} title={c.get('title')!r} "
                  f"字数={len(msgs[-1].get('content') or '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
