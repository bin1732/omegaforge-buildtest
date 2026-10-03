#!/usr/bin/env python3
"""回退校验：执行器必须真的清掉校验新建的临时样本文件。

## 被验的行为

有的校验不改写已有文件，而是新建一个带问题的样本文件再让扫描去找它。
这类文件是新增的，git 恢复删不掉。执行一旦在脚本清理之前中断，样本残留
就留在工作区：既看着像真实改动，又会让后续所有校验在执行前的"必须干净"
检查上被拒——报出来的全是"工作区不干净"，没有一个指向真实原因。

## 校验点

A 基线      造一个残留样本，经执行器跑完一趟后必须消失
B 清理失效  把清理规则清空，同样的残留跑完一趟必须还在

B 是防 A 走形式：只验 A 的话，清理规则被摘掉后 A 依然全绿（因为目标脚本
自己也会清理自己造的样本），而残留恰恰来自"脚本没机会清理"的情形。

用法：python3 probes/rev_runner_leftover.py [A|B|all]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "probes" / "rev_runner.py"
BACKUP = "/tmp/rev_runner_backup.py"
# 驱动一个耗时短且稳定的校验脚本，只为让执行器完整走一趟
DRIVEN = "probes/rev_orphan_reclaim.py"

# 残留样本：与整洁度扫描校验新建的文件同名同位置
SAMPLE = ROOT / "scripts" / "_rev_scope_probe_b.py"
SAMPLE_TEXT = '"""临时样本"""\n# 实测发现这里有问题\n'

# 注入：把清理规则清空
OLD_GLOBS = 'LEFTOVER_GLOBS = ("_rev_scope_probe_*.py",)'
NEW_GLOBS = "LEFTOVER_GLOBS = ()"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".revtmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _run_once() -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(RUNNER), DRIVEN, "110"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=200)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _guarded_dirty() -> list[str]:
    p = subprocess.run(["git", "status", "--porcelain", "--",
                        "omegaforge", "tests", "frontend/src"],
                       cwd=str(ROOT), capture_output=True, text=True)
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def heal() -> None:
    if Path(BACKUP).exists():
        shutil.copyfile(BACKUP, str(RUNNER))
        os.remove(BACKUP)
        print("  [自愈] 还原 rev_runner.py")
    if SAMPLE.exists():
        SAMPLE.unlink()
    # 被驱动的脚本一旦在注入态超时被杀，它自己造的源码改动会留在受守护
    # 目录里；下一次校验会在"执行前必须干净"上被拒，报出的却全是不相干的
    # 目录脏。所以自愈必须连带还原这些目录。
    dirty = _guarded_dirty()
    if dirty:
        subprocess.run(["git", "checkout", "--", "omegaforge", "tests",
                        "frontend/src"], cwd=str(ROOT),
                       capture_output=True, text=True)
        print(f"  [自愈] 还原受守护目录：{', '.join(dirty[:4])}")


def anchor_baseline() -> str | None:
    heal()
    pre = _guarded_dirty()
    if pre:
        return f"开工前受守护目录就不干净，本校验无法成立：{pre[:4]}"
    _atomic_write(SAMPLE, SAMPLE_TEXT)
    rc, log = _run_once()
    gone = not SAMPLE.exists()
    if gone:
        return None
    # 残留还在：说清是执行器拒跑（受守护目录脏）还是清理没生效
    tail = [l.strip() for l in log.splitlines() if l.strip()][-3:]
    return (f"跑完一趟后残留样本仍在（rc={rc}）"
            + (f"；受守护目录现为：{_guarded_dirty()[:4]}" if rc == 2 else "")
            + "；" + " | ".join(tail))


def anchor_sweep_removed() -> str | None:
    heal()
    src = RUNNER.read_text(encoding="utf-8")
    if OLD_GLOBS not in src:
        return "注入失败：执行器里找不到清理规则的锚点"
    shutil.copyfile(str(RUNNER), BACKUP)
    _atomic_write(RUNNER, src.replace(OLD_GLOBS, NEW_GLOBS, 1))
    try:
        _atomic_write(SAMPLE, SAMPLE_TEXT)
        _run_once()
        still = SAMPLE.exists()
    finally:
        shutil.copyfile(BACKUP, str(RUNNER))
        os.remove(BACKUP)
    if not still:
        return "清理规则被摘掉后残留依然消失：A 校验点没有真正验到清理"
    return None


ANCHORS = {
    "A": ("基线：残留被清掉", anchor_baseline),
    "B": ("清理失效：残留必须在", anchor_sweep_removed),
}


def main() -> int:
    heal()
    want = sys.argv[1] if len(sys.argv) > 1 else "all"
    keys = list(ANCHORS) if want == "all" else [want.upper()]
    bad = []
    for k in keys:
        name, fn = ANCHORS[k]
        reason = fn()
        if reason:
            bad.append(name)
            print(f"  [未抓到] {name}：{reason}")
        else:
            print(f"  [抓到] {name}")
        time.sleep(1)
    heal()
    print(f"\n校验点 {len(keys)} 个，精确抓到 {len(keys) - len(bad)} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
