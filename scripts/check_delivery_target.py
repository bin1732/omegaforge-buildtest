#!/usr/bin/env python3
"""交付脚本的目标仓库守卫。

推送脚本一旦把私人仓写死成默认目标，一次误调用就会在验证完成之前覆盖
交付物，而覆盖是强制的、没有回退。这类失效不会报错：脚本照常成功，
只是写错了地方。

守卫要求每个交付脚本同时满足：
  1. 目标仓库不写死为私人仓（默认值不得等于私人仓）；
  2. 存在私人仓的显式放行判据（否则即便默认值改了，仍可能被环境变量带过去）。

判定只看源码，不执行脚本——执行会产生真实推送。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = "bin1732/omegaforge"
SCRIPTS = [
    "scripts/push_via_api.py",
    "scripts/push_force_cover.py",
    "scripts/push_repo.py",
]


def check(path: str) -> list[str]:
    """返回该脚本存在的问题；空列表表示合规。"""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    problems: list[str] = []

    # 默认值等于私人仓：形如 os.environ.get("PUSH_REPO", "bin1732/omegaforge")
    # 或直接 REPO = "bin1732/omegaforge"。
    # 常量名须恰好是 REPO / PUSH_REPO：PRIVATE_REPO = "私人仓" 是判据本身，
    # 把它当成默认值会把守卫变成永远报错，进而被整段绕过。
    pat = (r'(?:(?<![A-Za-z_])REPO\s*=\s*"'
           + re.escape(PRIVATE)
           + r'"|get\(\s*"PUSH_REPO"\s*,\s*"' + re.escape(PRIVATE) + r'"\))')
    for _ in re.finditer(pat, src):
        problems.append(f"{path}: 目标仓库默认值写死为私人仓")

    # 显式放行判据：私人仓须经 ALLOW_PRIVATE_PUSH 之类判据才能推送。
    if PRIVATE in src and "ALLOW_PRIVATE_PUSH" not in src:
        problems.append(f"{path}: 出现私人仓但缺少显式放行判据")
    return problems


def main() -> int:
    found: list[str] = []
    for rel in SCRIPTS:
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            found.append(f"{rel}: 脚本缺失，无法判定目标是否受控")
            continue
        found.extend(check(full))
    if found:
        print("=== 交付脚本目标不受控 ===")
        for f in found:
            print("  ❌ " + f)
        return 1
    print(f"交付脚本目标守卫通过：{len(SCRIPTS)} 个脚本均需显式放行私人仓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
