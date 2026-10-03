"""端到端：验证 /api/jobs/<id> 在运行期能返回增量日志与进度。

这是用户截图"进度 0%、点了没反应"的直接回归测试。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


SOURCE = """You are ArxivResearcher, a meticulous academic research assistant.
Your mission: locate and summarize academic papers with rigor and precision.
Always cite sources with arXiv IDs. Never fabricate DOIs or page numbers.
You must verify every claim against at least one primary source before asserting it.
Tools available: arxiv_search(query), fetch_paper(id), extract_citations(text).
Workflow: parse intent -> search -> filter by relevance -> read abstracts -> synthesize.
Output format: markdown brief under 800 words with a sources section.
"""


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_api_")
  # 快照并还原：本套件若被 pytest 与其他测试同批执行，改掉 OMEGAFORGE_HOME
  # 又不还原，会让后续测试静默跑在错误的目录上。
  prev_env = os.environ.get("OMEGAFORGE_HOME")
  httpd = None
  try:
    from http.server import ThreadingHTTPServer
    from omegaforge.server import Handler
    import omegaforge.server as srv

    os.environ["OMEGAFORGE_HOME"] = tmp
    prev_home = srv.RUNS.home
    # 不再需要（也不能）手动同步 runs_dir：它已是只读 property，
    # 从 home 现算，赋 home 即自动跟随（见 core/paths.py）。
    srv.RUNS.home = tmp

    port = 8801
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    base = f"http://127.0.0.1:{port}"

    def post(path, obj):
      req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
      with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

    def get(path):
      with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)

    # ---------- 提交 ----------
    print("\n[1] 提交蒸馏任务")
    res = post("/api/distill", {"source": SOURCE, "budget": 300000,
                  "rounds": 2, "gens": 1})
    jid = res.get("job")
    check("返回 job id", bool(jid), str(jid))

    # ---------- 运行期轮询 ----------
    # 注意：mock 模式下整条流水线可在 0.2s 内跑完，轮询间隔若过大
    # 会直接跳到终态、观测不到中间进度。因此这里用细间隔 + 直接
    # 检查阶段序列（后者不依赖时序，是对阶段回调生效的本质验证）。
    print("\n[2] 运行期轮询（核心回归）")
    seen_logs, seen_progress, phases, cursor = [], [], [], 0
    for _ in range(400):
      time.sleep(0.02)
      d = get(f"/api/jobs/{jid}?since={cursor}")
      evs = d.get("events") or []
      if evs:
        cursor = evs[-1].get("seq", cursor)
        seen_logs.extend(e.get("message", "")
                 for e in evs if e.get("type") == "log")
        for e in evs:
          ph = e.get("phase")
          if ph and ph not in phases:
            phases.append(ph)
      seen_progress.append(d.get("progress", -1))
      if d.get("status") in ("done", "error", "cancelled"):
        break

    uniq_prog = sorted({p for p in seen_progress if p >= 0})
    check("运行期拿到日志", len(seen_logs) > 0, f"{len(seen_logs)} 条")
    check("进度最终到 100", max(uniq_prog or [0]) == 100, str(max(uniq_prog or [0])))
    # 阶段序列：证明 on_phase 回调真正驱动了阶段推进
    check("观测到多个阶段（进度可细分）", len(phases) >= 3,
       f"阶段序列 {phases}")
    expected = {"ingest", "extract", "compress", "synthesize",
          "gen_eval", "arena"}
    missing = sorted(expected - set(phases))
    check("关键阶段齐全", not missing,
       f"缺失 {missing}" if missing else "齐全")
    check("终态为 succeeded/failed",
       (phases[-1:] or [""])[0] in ("succeeded", "failed"),
       str(phases[-1:]))

    # ---------- 结果 ----------
    print("\n[3] 结果与可信度标注")
    fin = get(f"/api/jobs/{jid}")
    # status 保持旧契约（done/error），outcome 为精确语义
    check("status 兼容旧前端契约",
       fin.get("status") in ("done", "error"), str(fin.get("status")))
    check("outcome 为精确终态",
       fin.get("outcome") in ("succeeded", "failed"),
       str(fin.get("outcome")))
    check("含 verdict", "verdict" in fin, str(fin.get("verdict")))
    check("含 baseline_kind", "baseline_kind" in fin,
       str(fin.get("baseline_kind")))
    check("含 claim_valid", "claim_valid" in fin,
       str(fin.get("claim_valid")))
    check("genome 可读", isinstance(fin.get("genome"), dict)
       or "genome" not in fin)

    # ---------- 重启可恢复 ----------
    print("\n[4] 进程重启后可恢复（新 RunStore 读取磁盘）")
    from omegaforge.core.run import RunStore
    fresh = RunStore(home=tmp)
    got = fresh.get(jid)
    check("重启后能取回 run", got is not None)
    if got:
      check("事件仍在磁盘", len(got.events()) > 0,
         f"{len(got.events())} 条")

    # ---------- 列表 ----------
    print("\n[5] /api/runs 列表")
    rl = get("/api/runs")
    check("列表返回 runs", isinstance(rl.get("runs"), list),
       f"{len(rl.get('runs', []))} 个")
    check("列表项含 progress",
       all("progress" in r for r in rl.get("runs", [])))

    # ---------- 非法 id ----------
    print("\n[6] 非法 id 防护")
    code = 0
    try:
      get("/api/jobs/../../etc/passwd")
    except urllib.error.HTTPError as e:
      code = e.code
    check("路径穿越被拒（404/400）", code in (400, 404, 403), f"HTTP {code}")

  finally:
    # 不关服务会让 8801 一直被占：本套件单独跑能过，与别的套件同批跑就
    # 让后续测试拿到 "Address already in use"，属于测试基建污染而非产品缺陷。
    if httpd is not None:
      httpd.shutdown()
      httpd.server_close()
    srv.RUNS.home = prev_home
    if prev_env is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = prev_env
    shutil.rmtree(tmp, ignore_errors=True)

  print(f"\n{'='*46}")
  print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
  if FAIL:
    for f in FAIL:
      print(" FAILED:", f)
  return 1 if FAIL else 0




# ── pytest 入口 ──────────────────────────────────────────────────────
# 本文件原本只支持 `python3 tests/xxx.py` 独立运行，被 pytest 收集时
# 收集到 0 个用例（没有 test_ 函数），因此在 CI 里从未真正执行过——
# 守卫写了但不跑，等于没写。加这一层让它在两种入口下都跑真用例。
def test_api_live() -> None:
  assert main() == 0

if __name__ == "__main__":
  raise SystemExit(main())
