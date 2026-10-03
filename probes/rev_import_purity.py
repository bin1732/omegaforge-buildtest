#!/usr/bin/env python3
# -*- coding: 相关部分 -*-
"""校验：测试模块的「导入纯净度」守卫必须真能抓到回归。

判据不是"注入后测试失败"，而是"注入后测试**以 failed 结束**"。
（原先踩过：注入把文件改出 IndentationError，测试根本没跑起来，
 rc≠0 被误读成"抓到"。所以每次注入后强制 ast.parse 校验语法。）

校验点
----
A  给某个测试文件加回模块级 os.environ 赋值        -> 必须红
B  把 _legacy_sse_body.py 改回 test_sse_body.py    -> 必须红（可被通配捡到）
C  从 SUITES 里删掉一个 _legacy_ 脚本的登记        -> 必须红（没人执行它）
"""

from __future__ import annotations

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())

import ast
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = "tests/test_import_purity.py"

ENV_ASSIGN = '\nos.environ["OMEGAFORGE_HOME"] = "/tmp/rev_injected_home"\n'


def _run() -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", GUARD, "-q"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    tail = (p.stdout or "") + (p.stderr or "")
    return p.returncode, tail


def _failed_count(out: str) -> int:
    for line in out.splitlines():
        line = line.strip()
        if " failed" in line or line.startswith("FAILED"):
            pass
    import re
    m = re.findall(r"(\d+) failed", out)
    return int(m[-1]) if m else 0


def report(name: str, rc: int, out: str) -> bool:
    n = _failed_count(out)
    ok = rc != 0 and n > 0
    print(f"\n[{name}]")
    print(f"  rc={rc}  failed={n}  {'抓到' if ok else '未抓到'}")
    if out.strip():
        print("  " + out.strip().splitlines()[-1][:160])
    return ok


def main() -> int:
    print("=" * 74)
    print("第 45 章反向验证：测试模块导入纯净度")
    print("=" * 74)

    base_rc, base_out = _run()
    print(f"\n[基线] 未注入: rc={base_rc} failed={_failed_count(base_out)}")
    if base_rc != 0:
        print("基线就不干净，无法反向验证")
        return 1

    results = {}

    # --- A 模块级环境赋值 ---
    victim = ROOT / "tests" / "test_tools_param_caps.py"
    orig = victim.read_text(encoding="utf-8")
    try:
        victim.write_text(orig + ENV_ASSIGN, encoding="utf-8")
        ast.parse(victim.read_text(encoding="utf-8"))  # 语法必须仍然合法
        rc, out = _run()
        results["A 加回模块级 os.environ 赋值"] = report(
            "A 加回模块级 os.environ 赋值", rc, out)
    finally:
        victim.write_text(orig, encoding="utf-8")

    # --- B _legacy 改回 test_ 前缀 ---
    src = ROOT / "tests" / "_legacy_sse_body.py"
    dst = ROOT / "tests" / "test_sse_body.py"
    try:
        shutil.copy2(src, dst)
        rc, out = _run()
        results["B _legacy_sse_body 改回 test_ 前缀"] = report(
            "B _legacy_sse_body 改回 test_ 前缀", rc, out)
    finally:
        if dst.exists():
            dst.unlink()

    # --- C 从 SUITES 删掉一项 ---
    suites = ROOT / "tests" / "test_legacy_suites.py"
    orig_s = suites.read_text(encoding="utf-8")
    try:
        # 按行内容匹配，不写死缩进：缩进一改就匹配不上，锚点会退化成
        # AssertionError——看着是"抓到了失败"，实际是脚本崩了，C 从未验过。
        kept, dropped = [], 0
        for line in orig_s.splitlines(keepends=True):
            if "_legacy_sse_body.py" in line and line.strip().startswith("("):
                dropped += 1
                continue
            kept.append(line)
        assert dropped == 1, f"SUITES 里的登记项匹配到 {dropped} 行（期望 1 行）"
        removed = "".join(kept)
        suites.write_text(removed, encoding="utf-8")
        ast.parse(removed)
        rc, out = _run()
        results["C SUITES 删掉一项登记"] = report(
            "C SUITES 删掉一项登记", rc, out)
    finally:
        suites.write_text(orig_s, encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"反向验证: {sum(results.values())}/{len(results)} 锚点抓到")
    print("=" * 74)
    for k, v in results.items():
        print(f"  {'抓到' if v else '未抓到'}  {k}")

    final_rc, final_out = _run()
    print(f"\n[还原校验] rc={final_rc} failed={_failed_count(final_out)}")
    return 0 if all(results.values()) and final_rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
