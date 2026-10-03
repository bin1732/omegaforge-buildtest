#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块审计覆盖度表 —— 用「文档命中数」作为「是否被系统审计过」的代理指标。

为什么需要这张表
----------------
前 45 章的选题方式是"哪里冒出问题就打哪里"，属于机会驱动。机会驱动的代价
是**盲区不可见**：没人知道哪些大模块没有被审计过，直到它某天自己炸出来。

本表把 41 个产品模块按行数排序，并统计每个模块的文件名在 docs/第*章*.md
里出现过几次。命中 0 = 从未在任何一章里被点名 = 从未系统审计。

代理指标的局限（说明）
--------------------------
· "文档提到" ≠ "审计充分"：命中 4 次也可能只改了一处。
· 命中 0 也不绝对等于没测过：可能有测试文件覆盖，但没写进章节文档。
  所以本表的用途是**排序选题**，不是判定"安全/危险"。

运行：  python3 probes/probe_coverage_map.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "omegaforge"
DOCS = ROOT / "docs"

#: 已被章节点名审计过、无需再列入盲区的模块（手工登记，注明出处）
ACKNOWLEDGED: dict[str, str] = {
    "domain/baseline.py": "第 45 章（对照组）实测并修复，文档待补",
}


def main() -> int:
    docs = sorted(DOCS.glob("第*章*.md"))
    blobs = {d.name: d.read_text(encoding="utf-8") for d in docs}

    rows = []
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        rel = str(p.relative_to(ROOT))
        n = len(p.read_text(encoding="utf-8").splitlines())
        hit = sum(1 for b in blobs.values() if p.name in b)
        rows.append((n, rel, hit))
    rows.sort(key=lambda r: (-r[0], r[1]))

    print("=" * 78)
    print(f"模块审计覆盖度表   文档篇数={len(docs)}   模块数={len(rows)}"
          f"   总行数={sum(r[0] for r in rows)}")
    print("=" * 78)
    print(f"{'行数':>6}  {'命中':>4}  {'测试文件':>8}  模块")
    print("-" * 78)

    tests_dir = ROOT / "tests"
    for n, rel, hit in rows:
        name = pathlib.Path(rel).name
        stem = name[:-3]
        ntest = len(list(tests_dir.glob(f"test_*{stem}*.py"))) + \
            len(list(tests_dir.glob(f"test_*{stem[:-1]}*.py")))
        flag = "" if hit else "   <== 盲区"
        print(f"{n:>6}  {hit:>4}  {ntest:>8}  {rel}{flag}")

    blind = [r for r in rows if r[2] == 0]
    print("-" * 78)
    print(f"盲区（命中 0）：{len(blind)} 个，共 {sum(r[0] for r in blind)} 行")
    for n, rel, _ in blind:
        note = ACKNOWLEDGED.get(
            str(pathlib.Path(rel).relative_to("omegaforge")), "")
        print(f"  {n:>5} 行  {rel}" + (f"   [{note}]" if note else ""))

    top = [r for r in blind if r[0] >= 120]
    if top:
        print("\n建议优先审计（盲区中行数 >= 120）：")
        for n, rel, _ in top:
            print(f"  {n:>5} 行  {rel}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
