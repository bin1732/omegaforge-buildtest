#!/usr/bin/env python3
"""全接口在畸形入参下的报错文案体检。

## 为什么是这一层

报错是使用者能读到的最后一道文字。它只需要满足三件事：

1. 是中文——英文技术串照着无从下手；
2. 说清"缺什么 / 该填什么"，而不是"请检查后重试"——后者只会让人
   盲目重试，而重试多少次都不会成功；
3. 不回显内部字段名、模块名、英文枚举——那是内部产物。

只看代码无法判断：文案可能在某一层被统一转译覆盖，也可能根本没被
触发。所以逐个接口真发畸形入参，看真实返回。

用法：
    python3 probes/probe_error_text_sweep.py
退出码 0 = 全部接口文案达标；1 = 存在不达标项。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_sweep_")

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"

# 英文单词：界面上出现英文技术串即视为内部产物外泄。
# 允许少量通用缩写（API / URL / ID），它们已属日常用语。
ALLOWED_LATIN = {
    "api", "url", "id", "token", "json", "http", "https",
    "openai", "zhipu", "siliconflow", "deepseek", "mock",
    "genome", "distill", "omegaforge", "tauri", "mcp",
    # "源 Agent" 是全界面一致的正式叫法（见 ForgePage 的输入框标签），
    # 不是内部标识，不应改动。
    "agent",
}

# 内部字段名/模块名：这些一旦出现在报错里，就说明没做转译
INTERNAL_TOKENS = [
    "Traceback", "Error:", "Exception", "errno", "Errno",
    "NoneType", "KeyError", "ValueError", "TypeError",
    "OSError", "assert", "def ", "self.", "os.", "sys.",
    ".py", "/data/", "/home/", "omegaforge/", "omegaforge.",
]

# 无信息量的兜底文案：出现即说明该错误没被正确归类
VAGUE = [
    "操作失败，请稍后重试",
    "请求内容有误，请检查后重试",
    "服务器内部错误",
    "未知错误",
]

# 每个接口：路径 + 一组畸形入参。故意用错类型、空值、超长值。
CASES: list[tuple[str, list[dict]]] = [
    ("/api/chat", [{"conversation_id": 12345, "message": 3.5},
                   {"conversation_id": "x", "message": []}]),
    ("/api/chat/stream", [{"message": {"a": 1}}, {"message": None}]),
    ("/api/compare", [{"a": "x"}, {"models": "not-a-list"}]),
    ("/api/conversations/delete", [{"id": 999}, {"conversation_id": []}]),
    ("/api/conversations/model", [{"id": "x", "model": 7}]),
    ("/api/conversations/new", [{"title": 123}, {"title": ["a"]}]),
    ("/api/distill", [{"source": 5}, {"source": ""}, {"rounds": "many"}]),
    ("/api/kb/add", [{"title": 1, "content": 2}, {"title": "t"}]),
    ("/api/kb/search", [{"q": 5}, {"query": {}}]),
    ("/api/memory/recall", [{"q": 5}, {"query": []}]),
    ("/api/memory/remember", [{"content": 9}, {"content": {}}]),
    ("/api/providers/apply", [{"name": 5}, {"name": {}}]),
    ("/api/providers/models", [{"name": 5}, {"name": []}]),
    ("/api/providers/test", [{"name": 5}]),
    ("/api/skills/install", [{"path": 5}, {"path": {}}]),
    ("/api/skills/invoke", [{"name": 5}, {"name": {"a": 1}}]),
    ("/api/tasks/add", [{"text": 5}, {"text": {}}]),
    ("/api/tasks/done", [{"id": 5}, {"task_id": []}]),
    ("/api/tools/exec", [{"name": 5}, {"args": "notadict"}]),
    ("/api/tools/permissions", [{"scope": 5}]),
    ("/api/tools/policy", [{"policy": "not-a-valid-policy"}]),
    ("/api/voice/asr", [{"path": 5}]),
    ("/api/voice/tts", [{"text": 5}]),
    ("/api/wiki/page", [{"slug": 5}]),
    ("/api/wiki/save", [{"slug": 5, "content": 6}]),
]

# 只读接口：用畸形查询串而非 body
READ_CASES = [
    "/api/runs/../../etc",
    "/api/report/not-a-real-id",
    "/api/genome/not-a-real-id",
    "/api/jobs/not-a-real-id",
]


def post(path: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def error_text(status: int, body: str) -> str | None:
    """取出真实返回里的报错文案；非报错返回 None。"""
    if status < 400:
        return None
    try:
        obj = json.loads(body)
    except Exception:
        return None
    if isinstance(obj, dict):
        return str(obj.get("error") or obj.get("message") or "")
    return None


def verdict(text: str) -> list[str]:
    bad = []
    for tok in INTERNAL_TOKENS:
        if tok in text:
            bad.append(f"含内部产物 {tok!r}")
    for v in VAGUE:
        if v in text:
            bad.append(f"无信息量兜底 {v!r}")
    if not re.search(r"[一-鿿]", text):
        bad.append("非中文")
    words = set(re.findall(r"[A-Za-z]+", text))
    stray = {w for w in words if w.lower() not in ALLOWED_LATIN}
    if stray:
        bad.append(f"英文残留 {sorted(stray)[:4]}")
    return bad


def main() -> int:
    from omegaforge.server import Handler

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    problems: list[str] = []
    checked = 0
    try:
        for path, payloads in CASES:
            for payload in payloads:
                status, body = post(path, payload)
                text = error_text(status, body)
                if text is None:
                    continue
                checked += 1
                bad = verdict(text)
                if bad:
                    problems.append(f"{path} {json.dumps(payload, ensure_ascii=False)}"
                                    f"\n    文案: {text!r}\n    问题: {'; '.join(bad)}")
        for path in READ_CASES:
            status, body = get(path)
            text = error_text(status, body)
            if text is None:
                continue
            checked += 1
            bad = verdict(text)
            if bad:
                problems.append(f"{path}\n    文案: {text!r}\n    问题: {'; '.join(bad)}")
    finally:
        httpd.shutdown()

    print(f"已核对报错文案 {checked} 条")
    if problems:
        print(f"\n不达标 {len(problems)} 条：\n")
        for p in problems:
            print("  - " + p)
        return 1
    print("全部达标 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
