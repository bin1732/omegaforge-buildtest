"""检查脚本：进程重启后，未跑到终态的任务会停在什么状态。

场景：蒸馏在后台线程里跑（daemon），进程被关闭或崩溃时线程随之消失，
磁盘上的事件流停在中途，没有终态事件。重启后用户打开运行记录，
看到的是那条任务仍在"进行中"，界面一直轮询、一直等不到结果。

本检查脚本用一个独立进程写事件流（模拟被中断的任务），再用新进程读状态，
验证两件事：
  1. 重启后该任务的状态是什么
  2. 前端轮询是否永远等不到终态

运行：  python3 probes/probe_orphan_run.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程：写一个"进行中"的事件流然后立刻退出，不写终态
CHILD = textwrap.dedent("""
    import os, sys, json
    sys.path.insert(0, {root!r})
    os.environ["OMEGAFORGE_HOME"] = {home!r}
    from omegaforge.core.run import RunStore
    st = RunStore()
    r = st.create("demo", budget=50000, rounds=1, gens=1, task="中断演示")
    r.log("开始抽取")
    r.phase_to("extract", message="进入阶段 extract")
    r.log("正在处理")
    print(r.id)
""")

# 子进程：模拟重启后读取
READER = textwrap.dedent("""
    import os, sys, json
    sys.path.insert(0, {root!r})
    os.environ["OMEGAFORGE_HOME"] = {home!r}
    from omegaforge.core.run import RunStore
    st = RunStore()
    r = st.get({rid!r})
    if r is None:
        print(json.dumps({{"error": "读不到"}}))
    else:
        s = r.state()
        print(json.dumps({{
            "status": s.get("status"),
            "phase": s.get("phase"),
            "events": len(r.events()),
            "log": r.log_lines(),
        }}, ensure_ascii=False))
""")


def _run(code: str, home: str) -> str:
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120, cwd=ROOT,
                       env={**os.environ, "OMEGAFORGE_HOME": home})
    if p.returncode != 0:
        return f"[子进程失败 rc={p.returncode}] {p.stderr.strip()[-300:]}"
    return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""


def main() -> int:
    with tempfile.TemporaryDirectory() as home:
        rid = _run(CHILD.format(root=ROOT, home=home), home)
        if not rid or rid.startswith("["):
            print("创建失败:", rid)
            return 1
        print(f"模拟被中断的任务 id={rid}（事件流停在中途，无终态）")

        out = _run(READER.format(root=ROOT, home=home, rid=rid), home)
        print("\n=== 新进程（模拟重启后）读到 ===")
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            print("原始输出:", out)
            return 1
        print(json.dumps(d, ensure_ascii=False, indent=2))

        status = d.get("status")
        print()
        print("=== 判定 ===")
        print(f"状态 = {status}")
        if status in ("running", "queued"):
            print("结论：重启后该任务永久停在非终态，前端轮询永远等不到结果")
        else:
            print("结论：状态已归位，不会永久悬空")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
