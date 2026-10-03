#!/usr/bin/env python3
"""逐页像素校验：每一页都要真的画出内容，不只是"页面能打开"。

## 补的是什么

单看任一层都不够：

  * 逐页核查（verify_pages_browser.py）—— 验"页面能打开、有节点、有文案"
  * 像素校验（verify_pixel_render.py）—— 只截首屏

两层之间有一个空洞：**页面能打开且报出节点数，画面却可以是空白的。**
节点数来自 DOM，DOM 存在不代表渲染出了可见内容——元素可以全部零高度、
全部透明、被上层覆盖、或渲染在视口之外。这种情况逐页核查全绿，而用户
看到的是一片空白。

同时只截首屏是另一个缺口：它验到的是默认页，其余九页的渲染结果从未
被看过一眼。九页里任意一页白屏，这一层都发现不了。

本脚本把两者合起来：逐页点击切换 → 每页截图 → 每张图做像素判定。

## 判定

  * 主色占比不得过高（整屏同色 = 空白）
  * 边缘占比不得过低（没有相邻差异 = 没有内容）
  * **每一页都必须给出数值**——缺一张图就判失败，不得跳过

缺图跳过是这层最容易退化成的形态：截图失败时静默少一张，报告仍写
"全部通过"，而那正是白屏的那一页。

用法：python3 scripts/verify_pixels_per_page.py [--dist DIR] [--shots DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from verify_pixel_render import _analyze, MAX_DOMINANT_RATIO, MIN_EDGE_RATIO  # noqa: E402

EXPECTED_PAGES = [
    "蒸馏工坊", "对话", "知识库", "待办", "技能与人设",
    "竞技场", "基因组", "运行记录", "用量", "设置",
]


def analyze_shots(pages: list, shots_dir: str) -> tuple[list, list]:
    """对每页截图做像素判定，返回 (明细, 失败理由)。

    判定必须逐页独立给出结论，不能汇总成一个数：汇总后"九页正常一页
    白屏"与"十页正常"在总数上只差 1，而白屏那一页恰恰是需要被点名的。
    """
    rows = []
    fails = []
    for label in EXPECTED_PAGES:
        png = None
        for name in os.listdir(shots_dir) if os.path.isdir(shots_dir) else []:
            if name.endswith(".png") and _normalize(name[:-4]) == _normalize(label):
                png = os.path.join(shots_dir, name)
                break
        if png is None or not os.path.isfile(png):
            fails.append(f"{label}：缺截图，不得跳过"
                         f"（缺图静默放行会让白屏页被判通过）")
            rows.append((label, None, None, "缺图"))
            continue
        dom, edge, note = _analyze(png)
        rows.append((label, dom, edge, note))
        if dom > MAX_DOMINANT_RATIO:
            fails.append(f"{label}：画面接近纯色（主色占比 {dom:.3f}）—— "
                         "渲染空白，与白屏同义")
        if edge < MIN_EDGE_RATIO:
            fails.append(f"{label}：画面没有可辨内容（边缘占比 "
                         f"{edge:.4f}）—— 只渲染出壳")
    return rows, fails


def _normalize(s: str) -> str:
    """文件名由 label 过滤而来，非中英文字符被替换成下划线。"""
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or "\u4e00" <= ch <= "\u9fa5") else "_")
    return "".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="/data/workspace/fe_env/dist")
    ap.add_argument("--shots", default="/tmp/pageshot")
    ap.add_argument("--src", default=os.path.join(ROOT, "frontend", "src"))
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--build-root", default="/data/workspace/fe_env")
    args = ap.parse_args(argv)

    if os.path.isdir(args.shots):
        for name in os.listdir(args.shots):
            if name.endswith(".png"):
                os.remove(os.path.join(args.shots, name))
    os.makedirs(args.shots, exist_ok=True)

    cmd = [sys.executable, os.path.join(HERE, "verify_pages_browser.py"),
           "--dist", args.dist, "--shots", args.shots,
           "--src", args.src, "--build-root", args.build_root]
    if args.sync:
        cmd.append("--sync")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        print("FAIL: 逐页核查未通过，像素级校验无从执行")
        print((p.stdout or "")[-1500:])
        print((p.stderr or "")[-800:])
        return 1

    rows, fails = analyze_shots(EXPECTED_PAGES, args.shots)

    print(f"\n== 逐页像素校验（{len(EXPECTED_PAGES)} 页）==")
    for label, dom, edge, note in rows:
        if dom is None:
            print(f"  {label:<8} 缺图  {note}")
        else:
            print(f"  {label:<8} 主色={dom:.3f}  边缘={edge:.4f}  {note}")
    if fails:
        print("\n=== 逐页像素校验未通过 ===")
        for f in fails:
            print("  ❌ " + f)
        return 1
    print("\n=== 逐页像素校验通过：十页均画出可辨内容 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
