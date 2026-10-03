"""后端响应顶层形态验证表：逐个接口记录顶层类型与容器键。

用途：前端从未编译运行过，api.ts 声明的返回类型（数组 vs 对象、容器键名）
从未与真实响应对过账。本文件产出形态表，供契约比对使用。
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

GETS = [
  "/api/status", "/api/usage", "/api/providers",
  "/api/providers/models?name=openai", "/api/personas",
  "/api/tools/permissions", "/api/kb/list", "/api/tasks/list",
  "/api/wiki/list", "/api/skills/list", "/api/runs",
  "/api/conversations", "/api/voice/status",
]

POSTS = [
  ("/api/memory/recall", {"query": "x"}),
  ("/api/kb/search", {"query": "x"}),
  ("/api/conversations/new", {"title": "形态探测脚本"}),
]


def http(method, url, payload=None, timeout=20):
  data = None
  headers = {}
  if payload is not None:
    data = json.dumps(payload).encode()
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, headers=headers, method=method)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      raw = r.read().decode("utf-8", "replace")
      try:
        return r.status, json.loads(raw)
      except Exception:
        return r.status, {"__raw__": raw[:120]}
  except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8", "replace")
    try:
      return e.code, json.loads(raw)
    except Exception:
      return e.code, {"__raw__": raw[:120]}
  except Exception as e: # noqa: BLE001
    return 0, {"__err__": str(e)[:120]}


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_shape_")
  try:
    from http.server import ThreadingHTTPServer
    from omegaforge.server import Handler
    import omegaforge.server as srv

    os.environ["OMEGAFORGE_HOME"] = tmp
    srv.RUNS.home = tmp
    # runs_dir 已是只读 property，从 home 现算（core/run.py），不能赋值。
    # 上面 `RUNS.home = tmp` 已让 runs_dir 自动指向 tmp/runs，
    # 这行赋值既非法（AttributeError）也冗余，故删除。

    port = 8851
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.5)
    base = f"http://127.0.0.1:{port}"

    print(f"{'接口':<34} {'码':<4} {'顶层':<6} 容器键")
    print("-" * 78)
    rows = []
    for p in GETS:
      st, d = http("GET", f"{base}{p}")
      kind = "list" if isinstance(d, list) else "dict"
      keys = sorted(d.keys())[:6] if isinstance(d, dict) else []
      rows.append((p, st, kind, keys))
      print(f"{p:<34} {st:<4} {kind:<6} {keys}")
    for p, payload in POSTS:
      st, d = http("POST", f"{base}{p}", payload)
      kind = "list" if isinstance(d, list) else "dict"
      keys = sorted(d.keys())[:6] if isinstance(d, dict) else []
      rows.append((p, st, kind, keys))
      print(f"{p:<34} {st:<4} {kind:<6} {keys}")
    httpd.shutdown()
    json.dump(rows, open("/tmp/shape_rows.json", "w"), ensure_ascii=False)
    return 0
  finally:
    os.environ.pop("OMEGAFORGE_HOME", None)


if __name__ == "__main__":
  sys.exit(main())


def test_contract_script_executes() -> None:
  """让 pytest 真正收集并执行本脚本（接口响应形态探测）。

  缺少该约束时本文件是 `if __name__ == "__main__"` 脚本，pytest 收集到 **0 项**，
  整份检查从未进入过回归——缺少该约束时报的"通过"数字来自记忆而非验证。
  包装后由 tests/contract_runner.py 统一以子进程执行，验证脚本整体能跑通。
  """
  from tests.contract_runner import run_contract_script
  run_contract_script(__file__)
