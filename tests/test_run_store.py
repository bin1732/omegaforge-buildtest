"""Run 事件流测试 — 针对旧实现的两个致命缺陷做回归验证。

覆盖：
 1. 运行期可增量读到日志（旧实现恒为空）
 2. 事件立即落盘（非缓冲）
 3. 进程重启后可恢复（旧实现全丢）
 4. seq 单调递增、恢复后续写不冲突
 5. 路径穿越防护
 6. 进度百分比随阶段推进
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.core.run import RunStore, PHASE_PROGRESS # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_run_")
  try:
    store = RunStore(home=tmp)
    run = store.create("some source agent text", budget=1000)

    # ---------- 1. 运行期增量可读（核心回归） ----------
    print("\n[1] 运行期增量可读")
    observed: list[int] = []
    stop = threading.Event()

    def poller() -> None:
      """模拟前端每 50ms 轮询一次。"""
      cursor = 0
      while not stop.is_set():
        evs = run.events(since=cursor)
        if evs:
          cursor = evs[-1]["seq"]
          observed.append(len(evs))
        time.sleep(0.05)

    def producer() -> None:
      for i in range(10):
        run.log(f"step {i}", phase="extract")
        time.sleep(0.03)
      run.succeed(score=8.0)

    t1 = threading.Thread(target=poller, daemon=True)
    t2 = threading.Thread(target=producer, daemon=True)
    t1.start()
    t2.start()
    t2.join(timeout=15)
    time.sleep(0.2)
    stop.set()
    t1.join(timeout=5)

    check("运行期轮询能读到事件（非空）", len(observed) > 0,
       f"读到 {len(observed)} 批")
    # 关键：不能是"结束时一次性拿到 11 条"
    check("事件分批到达而非一次性", len(observed) >= 3,
       f"批次={len(observed)}，若=1 说明仍是结束后才可见")

    # ---------- 2. 立即落盘 ----------
    print("\n[2] 立即落盘（不缓冲）")
    r2 = store.create("disk check")   # create 自带 1 条 queued 事件
    base = len(r2.events())
    r2.log("hello")
    # 不 join 任何东西，直接读文件（不经过 events() 缓存）
    with open(r2.path, encoding="utf-8") as f:
      on_disk = [json.loads(x) for x in f if x.strip()]
    check("emit 后磁盘立即多出 1 条", len(on_disk) == base + 1,
       f"磁盘 {len(on_disk)} 条（基线 {base}）")
    check("落盘内容正确", on_disk and on_disk[-1]["message"] == "hello")
    check("落盘带 phase 字段", on_disk and "phase" in on_disk[-1])

    # ---------- 3. 重启可恢复 ----------
    print("\n[3] 进程重启后可恢复")
    r3 = store.create("persist me")   # +1 queued
    r3.log("alpha")           # +1
    r3.phase_to("arena")         # +1
    r3.set_meta(verdict="win")
    rid = r3.id
    expect_n = 3

    store2 = RunStore(home=tmp)     # 模拟重启：全新 store
    got = store2.get(rid)
    check("重启后能取回 Run", got is not None)
    if got:
      check("恢复后事件完整", len(got.events()) == expect_n,
         f"{len(got.events())} 条（期望 {expect_n}）")
      check("恢复后 phase 保留", got.phase == "arena", got.phase)
      check("恢复后 meta 保留", got._meta.get("verdict") == "win")
      # 续写不冲突
      before = len(got.events())
      got.log("beta")
      after = len(got.events())
      check("恢复后续写 seq 不冲突", after == before + 1,
         f"{before} -> {after}")
      seqs = [e["seq"] for e in got.events()]
      check("seq 全局单调递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
         f"{seqs}")

    # ---------- 4. 路径穿越防护 ----------
    print("\n[4] 路径穿越防护")
    for bad in ("../../etc", "..", "abc", "", "a" * 200, "12G"):
      check(f"拒绝非法 id {bad[:12]!r}", store.get(bad) is None)

    # ---------- 5. 进度映射 ----------
    print("\n[5] 进度随阶段推进")
    r5 = store.create("progress")
    p0 = r5.progress
    r5.phase_to("extract")
    p1 = r5.progress
    r5.phase_to("arena")
    p2 = r5.progress
    r5.succeed()
    p3 = r5.progress
    check("进度单调递增", p0 < p1 < p2 < p3, f"{p0}->{p1}->{p2}->{p3}")
    check("完成态进度为 100", p3 == 100, str(p3))
    check("所有阶段都有进度值",
       all(p in PHASE_PROGRESS for p in
         ("ingest", "extract", "compress", "synthesize",
          "gen_eval", "arena", "evolve", "finalize")))

    # ---------- 6. 半行容错 ----------
    print("\n[6] 半行容错（进程被杀）")
    r6 = store.create("truncate")    # +1 queued
    r6.log("good")            # +1
    before_n = len(r6.events())
    with open(r6.path, "a", encoding="utf-8") as f:
      f.write('{"seq":99,"ts":1,"type":"log","ph')  # 截断的半行
      f.write("\n")
      f.write("not json at all\n")          # 完全非法行
    after = r6.events()
    check("半行不导致读取崩溃", len(after) == before_n,
       f"{len(after)} 条（期望 {before_n}）")
    check("截断行被丢弃而非污染",
       all(e.get("seq") != 99 for e in after),
       "seq=99 的半行未进入结果")
    check("合法事件仍可读", any(e.get("message") == "good" for e in after))

    # ---------- 7. 列表 ----------
    print("\n[7] 列表")
    items = store.list()
    check("能列出 run", len(items) >= 4, f"{len(items)} 个")
    check("列表含 progress 字段",
       all("progress" in i for i in items))

  finally:
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
def test_run_store() -> None:
  assert main() == 0

if __name__ == "__main__":
  raise SystemExit(main())
