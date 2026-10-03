#!/usr/bin/env python3
"""表层守卫（第二道）：全部 API 端点的真实响应键必须有中文标签。

## 与 check_labels.py 的分工

check_labels.py 只查 4 个 dataclass（Genome / DistillReport / Baseline /
RunStore）。但用户看到的不止这四处——36 个端点的响应里还有大量键名
（providers、skills、tasks、memory…），漏一个就把英文摆到用户面前。

本脚本不再依赖"手写清单"，而是**真实启动后端、逐个打端点、递归抽取
响应里的每一个键**，再与 labels.ts 对照。后端加字段而前端没补翻译，
这里直接失败。

## 为什么必须真实启动

键名若靠静态分析源码猜，会与实际响应脱节（动态构造的 dict 抽不到，
已删除的分支抽得到）。真实启动 + 真实请求是唯一可靠的来源。

退出码 0 = 全部覆盖；1 = 有未翻译字段。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

TS_LABELS = os.path.join(ROOT, "frontend", "src", "lib", "labels.ts")

# 真实存在的 GET 端点（从 omegaforge/server.py 路由表抄录，非臆造）
GET_ENDPOINTS = [
    "/api/status",
    "/api/usage",
    "/api/tools/permissions",
    "/api/providers",
    # 检验修正：该端点的查询参数是 name= 而非 provider=，
    # 用错参数会返回 404，把"我写错"误报成"后端有 bug"。
    "/api/providers/models?name=openai",
    "/api/personas",
    "/api/conversations",
    "/api/kb/search?q=test",
    "/api/kb/list",
    "/api/wiki/list",
    "/api/wiki/page?title=test",
    "/api/tasks/list",
    "/api/skills/list",
    "/api/memory/recall?q=test",
    "/api/runs",
    # /api/voice/status 是 POST 端点（见 server.py do_POST 分支），
    # 不在 GET 路由里，列进这里只会产生假失败。
]

# 这些键是协议字段或纯数据容器，不直接渲染给用户，允许无标签
PROTOCOL_KEYS = {
    "ok",           # 所有响应的成功标志
}


def collect_keys(obj, out: set, prefix: str = "") -> None:
    """递归抽取 JSON 里出现的每一个键名。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.add(k)
            collect_keys(v, out)
    elif isinstance(obj, list):
        for item in obj[:50]:  # 只取前 50 条，避免大列表拖慢
            collect_keys(item, out)


def frontend_labels() -> set:
    src = open(TS_LABELS, encoding="utf-8").read()
    m = re.search(r"const FIELD_LABELS:\s*Record<string,\s*string>\s*=\s*\{(.*?)\n\}",
                  src, re.S)
    if not m:
        raise SystemExit("labels.ts: 找不到 FIELD_LABELS 定义")
    body = m.group(1)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.M))


def wait_boot(port: int, timeout: float = 40.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                return True
        except OSError:
            time.sleep(0.6)
    return False


def main() -> int:
    port = 8791  # 与 CI 其他步骤错开，避免端口争抢
    env = dict(os.environ)
    # 端口变量名与 sidecar 一致（OF_PORT），否则后端仍会去 8787
    env["OF_PORT"] = str(port)
    # sidecar_main 把自身所在目录（scripts/）加入 sys.path，但 omegaforge
    # 包在仓库根——不设 PYTHONPATH 会 ModuleNotFoundError。
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")

    # 必须走 sidecar 入口：它负责把 stdout 强制为 UTF-8（Windows cp1252 下
    # 打印 Ω/中文会崩），直接 import server 在 CI 上会启动即退出。
    entry = os.path.join(ROOT, "scripts", "sidecar_main.py")
    proc = subprocess.Popen(
        [sys.executable, entry],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not wait_boot(port):
            print("后端未能启动 —— 守卫无法执行（不启动不等于通过）")
            return 1

        seen: set = set()
        ok, failed = 0, []
        for ep in GET_ENDPOINTS:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{ep}", timeout=10
                ) as f:
                    data = json.loads(f.read())
                collect_keys(data, seen)
                ok += 1
            except urllib.error.HTTPError as e:
                # 404/500 说明端点不存在或出错，属于后端问题，单独记录
                failed.append((ep, f"HTTP {e.code}"))
            except Exception as e:
                failed.append((ep, str(e)[:60]))

        have = frontend_labels()
        missing = sorted(k for k in seen if k not in have and k not in PROTOCOL_KEYS)

        print(f"实际打通端点 {ok}/{len(GET_ENDPOINTS)} 个")
        if failed:
            print("未打通（后端问题，需单独修）：")
            for ep, why in failed:
                print(f"  {ep}  ← {why}")
        print(f"响应中出现的键名 {len(seen)} 个，已翻译 {len(seen) - len(missing)} 个")

        if missing:
            print("\n未翻译字段（会原样显示英文给用户）：")
            for k in missing:
                print(f"  {k}")
            print("\n请在 frontend/src/lib/labels.ts 的 FIELD_LABELS 中补齐。")
            return 1
        print("全部覆盖 ✓")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
