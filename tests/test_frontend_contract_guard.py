"""前后端契约守卫：后端响应形态 × 前端解包，两侧都必须对得上。

背景：前端至今没有编译运行过，api.ts 自称"契约来源 server.py，逐一核对后录入"，
这句话未被真实请求验证。验证发现两个方向的错：

 1. 后端返回包装对象 {"runs": [...]}，前端按裸数组声明 ——
   三个主页面（运行记录/竞技场/基因组）runs.length === undefined、
   runs.map → TypeError，页面直接打不开。
 2. 前端按 /api/chat 的约定发 conversation_id，/api/conversations/delete
   与 /api/conversations/model 却只读 id —— 删除恒定 400「请求内容有误」，
   用户点了得不到任何指向输入框的提示，重试永远不会成功。

守卫分两类：
 A. 后端形态（真实 HTTP）——防止后端悄悄改成裸数组而前端没跟着改
 B. 前端解包（源码断言）——防止前端改回裸数组假设而后端没跟着改
两者互为表里，只测一侧等于没测。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FRONTEND_API = os.path.join(ROOT, "frontend", "src", "lib", "api.ts")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}"
     + (f" — {detail}" if detail else ""))


def http(method, url, payload=None, timeout=20):
  data = None
  headers = {}
  if payload is not None:
    data = json.dumps(payload).encode()
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, headers=headers, method=method)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      return r.status, json.loads(r.read().decode("utf-8", "replace"))
  except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8", "replace")
    try:
      return e.code, json.loads(raw)
    except Exception:
      return e.code, {"__raw__": raw[:160]}
  except Exception as e: # noqa: BLE001
    return 0, {"__err__": str(e)[:160]}


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_fecontract_")
  # 次序要紧：CONVS/RUNS 在 import 时就按 OMEGAFORGE_HOME 定好目录，
  # 先 import 再设环境变量，测试会静默跑在真实 .omegaforge 上
  # （读出 128 条会话，并且在真实目录里增删）。
  os.environ["OMEGAFORGE_HOME"] = tmp
  try:
    from http.server import ThreadingHTTPServer
    from omegaforge.server import Handler
    import omegaforge.server as srv

    srv.RUNS.home = tmp
    # runs_dir 已是只读 property，从 home 现算（core/run.py），不能赋值。
    # 上面 `RUNS.home = tmp` 已让 runs_dir 自动指向 tmp/runs，
    # 这行赋值既非法（AttributeError）也冗余，故删除。

    port = 8861
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.5)
    base = f"http://127.0.0.1:{port}"

    # ---------- A. 后端真实形态 ----------
    st, d = http("GET", f"{base}/api/runs")
    check("A1 /api/runs 是包装对象", isinstance(d, dict) and "runs" in d,
       f"顶层={type(d).__name__}")
    # 形态必须是 dict 才谈得上 .get —— 否则后端改成裸数组时这里
    # 会抛 AttributeError 让整个守卫崩掉，那是"崩了"不是"抓到了"。
    check("A2 /api/runs.runs 是数组",
       isinstance(d, dict) and isinstance(d.get("runs"), list))

    st, d = http("GET", f"{base}/api/conversations")
    check("A3 /api/conversations 是包装对象",
       isinstance(d, dict) and "conversations" in d)

    st, d = http("GET", f"{base}/api/providers/models?name=openai")
    check("A4 providers/models 给 catalog（不是 models）",
       isinstance(d.get("catalog"), list) and "models" not in d,
       f"keys={sorted(d.keys()) if isinstance(d, dict) else d}")

    st, d = http("GET", f"{base}/api/providers")
    check("A5 providers.active 允许为 null（全新安装）",
       "active" in d, f"active={d.get('active')!r}")

    # 会话：两个名字都要认，且要真的删掉
    st, d = http("POST", f"{base}/api/conversations/new", {"title": "契约"})
    cid = d.get("id") if isinstance(d, dict) else None
    st, d = http("POST", f"{base}/api/conversations/model",
           {"conversation_id": cid, "model": "gpt-4o-mini"})
    check("A6 model 接受 conversation_id", st == 200 and d.get("model_pref") == "gpt-4o-mini",
       f"got {st} {str(d)[:80]}")
    st, d = http("POST", f"{base}/api/conversations/model", {"id": cid, "model": "gpt-4o"})
    check("A7 model 仍接受 id（向后兼容）", st == 200, f"got {st}")
    st, d = http("POST", f"{base}/api/conversations/model", {"model": "x"})
    check("A8 model 缺对话时点名", st == 400 and "对话" in str(d),
       f"got {st} {str(d)[:80]}")

    before = len(http("GET", f"{base}/api/conversations")[1].get("conversations", []))
    st, d = http("POST", f"{base}/api/conversations/delete", {"conversation_id": cid})
    after = len(http("GET", f"{base}/api/conversations")[1].get("conversations", []))
    check("A9 delete 接受 conversation_id 且真删掉",
       st == 200 and d.get("deleted") is True and after == before - 1,
       f"got {st} deleted={d.get('deleted')} {before}->{after}")

    httpd.shutdown()
  finally:
    os.environ.pop("OMEGAFORGE_HOME", None)

  # ---------- B. 前端解包（源码断言） ----------
  src = ""
  if os.path.exists(FRONTEND_API):
    with open(FRONTEND_API, encoding="utf-8") as f:
      src = f.read()
  check("B0 找到前端 api.ts", bool(src))

  m = re.search(r"export const fetchRuns[\s\S]{0,400}?\n}", src)
  body = m.group(0) if m else ""
  check("B1 fetchRuns 解包 .runs", ".runs" in body and "Array.isArray" in body,
     body[:90].replace("\n", " "))

  m = re.search(r"export const fetchConversations[\s\S]{0,400}?\n}", src)
  body = m.group(0) if m else ""
  check("B2 fetchConversations 解包 .conversations",
     ".conversations" in body and "Array.isArray" in body,
     body[:90].replace("\n", " "))

  check("B3 ProvidersResponse.active 声明可为 null",
     "active: string | null" in src)

  print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
  if FAIL:
    print("失败项：" + ", ".join(FAIL))
  return 1 if FAIL else 0


if __name__ == "__main__":
  sys.exit(main())


def test_contract_script_executes() -> None:
  """让 pytest 真正收集并执行本脚本（13 项前端类型契约）。

  缺少该约束时本文件是 `if __name__ == "__main__"` 脚本，pytest 收集到 **0 项**，
  整份检查从未进入过回归——缺少该约束时报的"通过"数字来自记忆而非验证。
  包装后由 tests/contract_runner.py 统一以子进程执行，验证脚本整体能跑通。
  """
  from tests.contract_runner import run_contract_script
  run_contract_script(__file__)
