#!/usr/bin/env python3
"""全量回归的统一入口。

## 为什么需要单独一个入口

pytest 装在工作区的 `.pylibs` 里，不在默认搜索路径上。直接调用会以
`No module named pytest` 退出，而这个结果与"用例规模过大跑不完"在输出上
无法区分——两者都表现为没有结果。入口先补齐搜索路径再执行，于是"跑不完"
这一判断只能成立于环境已就绪之后。

## 判定

 · 收集到 0 项：单独报出并判失败。pytest 对"没收到用例"给的是退出码 5，
   与"有用例但失败"不同，混在一起看不出是路径写错还是真失败。
 · 有失败：以非 0 退出，并把失败用例名列出来。
 · 无论成败都打印用例总数与耗时，避免"通过了多少"这种没有基数的说法。

## 用法

    python3 probes/run_full_regression.py            # 全量
    python3 probes/run_full_regression.py --collect  # 只收集，不执行
    python3 probes/run_full_regression.py --dir tests/test_x.py
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "tests"


def _env() -> dict:
    sys.path.insert(0, str(ROOT))
    from probes._pytest_env import env
    return env()


def _run(args: list[str], env: dict, timeout: int):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR, help="用例目录或文件")
    ap.add_argument("--collect", action="store_true", help="只收集不执行")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    env = _env()
    print(f"PYTHONPATH 已补齐：{env.get('PYTHONPATH', '(沿用当前环境)')}")

    t0 = time.time()
    c = _run([args.dir, "--collect-only", "-q"], env, 300)
    m = re.search(r"(\d+)\s+tests?\s+collected", c.stdout + c.stderr)
    total = int(m.group(1)) if m else 0
    print(f"收集到 {total} 项（{args.dir}）")
    # 收集为 0 必须单独判：**空结果和"用例全过"在汇总行上都表现为没有失败
    # 项**。路径写错、目录名改了都会走到这里，若只按"有没有失败"判定，会被
    # 读成全量通过。
    if total == 0:
        print("未通过：没收集到任何用例，目录或文件名可能已变更")
        print((c.stdout + c.stderr).strip()[-400:])
        return 1
    if args.collect:
        print(f"用时 {time.time() - t0:.1f} 秒（仅收集）")
        return 0

    p = _run([args.dir, "-q", "-rf"], env, args.timeout)
    out = p.stdout + p.stderr
    dur = time.time() - t0
    tail = [l for l in out.splitlines() if re.search(r"\d+ (passed|failed)", l)]
    print((tail[-1] if tail else out.strip().splitlines()[-1]).strip())
    print(f"用时 {dur:.1f} 秒")

    if p.returncode != 0:
        failed = [l.strip() for l in out.splitlines() if l.startswith("FAILED")]
        print(f"\n未通过（rc={p.returncode}，共 {total} 项）")
        for l in failed[:20]:
            print("  " + l)
        return 1
    print(f"\n通过：{total} 项全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
