"""前端契约验证：把 frontend/src/lib/api.ts 声明的字段，逐一打到真实后端上验证。

背景：前端至今没有编译运行过，api.ts 头部自称"契约来源 server.py，逐一核对后录入"——
这句话未被真实请求验证过。本文件不依赖前端构建，直接起真实服务逐字段核对。

原则：只验证"前端声明它会读的字段"，不验证后端返回的所有字段。
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

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def http(method: str, url: str, payload=None, timeout=20):
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
        return r.status, {"__raw__": raw[:200]}
  except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8", "replace")
    try:
      return e.code, json.loads(raw)
    except Exception:
      return e.code, {"__raw__": raw[:200]}
  except Exception as e: # noqa: BLE001
    return 0, {"__err__": f"{type(e).__name__}: {e}"[:200]}


def has(d, *path):
  """按路径检查字段存在且非 None。"""
  cur = d
  for p in path:
    if not isinstance(cur, dict) or p not in cur or cur[p] is None:
      return False
    cur = cur[p]
  return True


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_fecontract_")
  prev_env = os.environ.get("OMEGAFORGE_HOME")
  # 次序要紧：store 在 import 时按 OMEGAFORGE_HOME 定目录。
  os.environ["OMEGAFORGE_HOME"] = tmp
  try:
    from http.server import ThreadingHTTPServer
    from omegaforge.server import Handler
    import omegaforge.server as srv

    srv.RUNS.home = tmp
    # runs_dir 已是只读 property，从 home 现算（core/run.py），不能赋值。
    # 上面 `RUNS.home = tmp` 已让 runs_dir 自动指向 tmp/runs，
    # 这行赋值既非法（AttributeError）也冗余，故删除。

    port = 8831
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.5)
    base = f"http://127.0.0.1:{port}"

    # --- BackendStatus ---
    st, d = http("GET", f"{base}/api/status")
    check("status 200", st == 200, f"got {st}")
    for f in ("version", "mock_mode", "jobs"):
      check(f"status.{f}", has(d, f), repr(d.get(f))[:60] if isinstance(d, dict) else "")
    for f in ("main", "fast", "judge"):
      check(f"status.models.{f}", has(d, "models", f))
    for f in ("limit", "spent", "remaining"):
      check(f"status.budget.{f}", has(d, "budget", f))

    # --- usage ---
    st, d = http("GET", f"{base}/api/usage")
    check("usage 200", st == 200, f"got {st}")

    # --- providers ---
    st, d = http("GET", f"{base}/api/providers")
    check("providers 200", st == 200, f"got {st}")
    for f in ("presets", "current"):
      check(f"providers.{f}", has(d, f))
    # active 在全新安装（无 providers.json）时为 null —— 键要在，值可空
    check("providers.active 键存在（值可 null）",
       isinstance(d, dict) and "active" in d, repr(d.get("active")))

    st, d = http("GET", f"{base}/api/providers/models?name=openai")
    check("providers/models 200", st == 200, f"got {st} {str(d)[:80]}")

    # --- distill 启动：前端按 {job, run} 取 ---
    st, d = http("POST", f"{base}/api/distill",
           {"source_prompt": "You are a careful research assistant. "
                    "Always cite sources and verify claims.",
           "task": "契约验证", "budget": 2000, "rounds": 1, "gens": 1})
    check("distill 200", st == 200, f"got {st} {str(d)[:120]}")
    job = d.get("job") if isinstance(d, dict) else None
    run = d.get("run") if isinstance(d, dict) else None
    check("distill.job 非空", bool(job), repr(job))
    check("distill.run 非空", bool(run), repr(run))

    # --- runs 列表项 ---
    deadline = time.time() + 25
    runs = []
    while time.time() < deadline:
      st, d = http("GET", f"{base}/api/runs")
      runs = d.get("runs") if isinstance(d, dict) else []
      if runs:
        break
      time.sleep(0.5)
    check("runs.runs 为列表且非空", isinstance(runs, list) and len(runs) > 0, f"{len(runs)} 条")
    if runs:
      check("runs[0].id", has(runs[0], "id"))
      check("runs[0].status", has(runs[0], "status"))

    # --- jobs / report / genome ---
    if job:
      st, d = http("GET", f"{base}/api/jobs/{job}")
      check("jobs/<id> 200", st == 200, f"got {st}")
      # 产物要等任务跑完才有 —— 跑完之前 404 是正确行为，不是缺陷。
      deadline = time.time() + 40
      done = False
      while time.time() < deadline:
        _, jd = http("GET", f"{base}/api/jobs/{job}")
        if isinstance(jd, dict) and jd.get("status") in ("done", "failed", "completed"):
          done = True
          break
        time.sleep(0.6)
      check("任务在 40s 内结束", done,
         f"status={jd.get('status') if isinstance(jd, dict) else jd}")
      for p in (f"{base}/api/report/{job}", f"{base}/api/genome/{job}"):
        st2, d2 = http("GET", p)
        check(f"{p.split('/api/')[1]} 200", st2 == 200, f"got {st2}")

    # --- conversations ---
    st, d = http("POST", f"{base}/api/conversations/new", {"title": "契约验证"})
    check("conversations/new 200", st == 200, f"got {st}")
    cid = d.get("id") if isinstance(d, dict) else None
    check("conversations/new.id", bool(cid), repr(cid))
    st, d = http("GET", f"{base}/api/conversations")
    check("conversations 列表 200", st == 200, f"got {st}")
    if cid:
      st, d = http("POST", f"{base}/api/conversations/model",
             {"conversation_id": cid, "model": "gpt-4o-mini"})
      check("conversations/model 200", st == 200, f"got {st} {str(d)[:80]}")

    # --- personas / permissions ---
    st, d = http("GET", f"{base}/api/personas")
    check("personas 200", st == 200, f"got {st}")
    st, d = http("GET", f"{base}/api/tools/permissions")
    check("permissions.permissions", has(d, "permissions"), f"got {st} {str(d)[:80]}")

    # --- 记忆 / 知识库 / 任务 / wiki ---
    st, d = http("POST", f"{base}/api/memory/remember", {"fact": "契约验证记忆", "tags": ["t"]})
    check("memory/remember 200", st == 200, f"got {st} {str(d)[:80]}")
    st, d = http("POST", f"{base}/api/memory/recall", {"query": "契约"})
    check("memory/recall 200", st == 200, f"got {st}")

    st, d = http("GET", f"{base}/api/kb/list")
    check("kb/list 200", st == 200, f"got {st}")
    st, d = http("POST", f"{base}/api/kb/add", {"title": "契约", "text": "契约正文"})
    check("kb/add 200", st == 200, f"got {st} {str(d)[:80]}")
    st, d = http("POST", f"{base}/api/kb/search", {"query": "契约"})
    check("kb/search 200", st == 200, f"got {st}")

    st, d = http("GET", f"{base}/api/tasks/list")
    check("tasks/list 200", st == 200, f"got {st}")
    st, d = http("POST", f"{base}/api/tasks/add", {"text": "契约验证任务"})
    check("tasks/add 200", st == 200, f"got {st} {str(d)[:80]}")

    st, d = http("GET", f"{base}/api/wiki/list")
    check("wiki/list 200", st == 200, f"got {st}")
    st, d = http("POST", f"{base}/api/wiki/save",
           {"slug": "contract-probe", "title": "契约", "body": "正文"})
    check("wiki/save 200", st == 200, f"got {st} {str(d)[:80]}")

    # --- skills / voice ---
    st, d = http("GET", f"{base}/api/skills/list")
    check("skills/list 200", st == 200, f"got {st}")
    st, d = http("GET", f"{base}/api/voice/status")
    check("voice/status 200", st == 200, f"got {st}")

    httpd.shutdown()
  finally:
    if prev_env is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = prev_env

  print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
  if FAIL:
    print("失败项：" + ", ".join(FAIL))
  return 1 if FAIL else 0


if __name__ == "__main__":
  sys.exit(main())


def test_contract_script_executes() -> None:
  """让 pytest 真正收集并执行本脚本（43 项前端接口契约）。

  缺少该约束时本文件是 `if __name__ == "__main__"` 脚本，pytest 收集到 **0 项**，
  整份检查从未进入过回归——缺少该约束时报的"通过"数字来自记忆而非验证。
  包装后由 tests/contract_runner.py 统一以子进程执行，验证脚本整体能跑通。
  """
  from tests.contract_runner import run_contract_script
  run_contract_script(__file__)
