"""抓真后端的错误响应体，交给前端渲染层核对文案是否原样展示。

后端文案写得再好，只要前端这一层把它换掉，用户看到的仍是另一句。
本脚本把真后端在畸形入参下的**真实响应体**落盘，由
probes/frontend_render/error_text_roundtrip.cjs 用真实前端的
userMessage() 转换，断言结果仍等于后端原文。

输出：/tmp/fe/error_responses.json
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

PORT = 8807
BASE = f"http://127.0.0.1:{PORT}"
OUT = "/tmp/fe/error_responses.json"

CASES = [
    ("/api/distill", {}),
    ("/api/chat", {}),
    ("/api/conversations/delete", {}),
    ("/api/conversations/model", {}),
    ("/api/voice/tts", {}),
    ("/api/voice/tts", {"text": "hi", "speed": "fast"}),
    ("/api/tools/policy", {"mode": "weird"}),
    ("/api/tools/exec", {}),
    ("/api/kb/add", {}),
    ("/api/wiki/save", {}),
    ("/api/tasks/add", {}),
    ("/api/tasks/add", {"text": "t", "priority": "high"}),
    ("/api/tasks/done", {}),
    ("/api/skills/install", {}),
    ("/api/skills/invoke", {}),
    ("/api/memory/remember", {}),
    ("/api/providers/apply", {}),
    ("/api/providers/apply", {"name": "openai", "models": "x"}),
    ("/api/providers/test", {}),
]


def main() -> int:
    home = "/tmp/errtxt_home"
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = home
    os.environ["OMEGAFORGE_MODE"] = "auto_edit"

    import omegaforge.server as srv
    srv.RUNS.home = None
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    out = []
    try:
        for path, payload in CASES:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                BASE + path, data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    code, raw = r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                code, raw = e.code, e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"__raw__": raw}
            out.append({"path": path, "payload": payload,
                        "status": code, "body": parsed})
    finally:
        httpd.shutdown()

    os.makedirs("/tmp/fe", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"已写入 {OUT}（{len(out)} 条）")
    for r in out:
        msg = r["body"].get("error") or r["body"].get("message") or ""
        print(f"  {r['path']} {r['status']} {str(msg)[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
