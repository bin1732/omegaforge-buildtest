#!/usr/bin/env python3
"""工具入口文案守卫的回退校验：注入回退后，对应用例必须变红。

为什么需要：
    守卫"通过"有两种可能——它真的在验，或者它恒真。判断依据是把它要
    拦的改动注入回去，看它是否变红。注入后先做语法校验：语法坏掉的
    文件会让测试直接崩，"崩"与"抓到"在输出里都表现为失败，不校验会把
    假结果当成证据。

写入一律走临时文件 + 原子替换并把"读回等于写入内容"当作成功判据：
    原地改写同一文件时，写入调用返回成功后读回仍可能是旧内容，那样
    "注入生效"的确认会失真，校验点结论也就不可信。

用法：
    python3 probes/rev_tool_dispatch_text.py
退出码 0 = 全部校验点抓到；1 = 至少一个校验点没抓到。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "omegaforge" / "tools" / "system_tools.py"
CASE = "tests/test_tool_dispatch_text.py"

GUARD = ("    if not isinstance(args, dict):\n"
         "        raise UserError(\"工具参数格式不正确，请填写一组参数\")\n")
RAISE = ("    raise UserError(f\"没有名为「{name}」的工具，\"\n"
         "                    f\"可用工具：{'、'.join(known)}\")\n")
KNOWN = ("    known = (\"运行终端命令\", \"读取本地文件\", \"写入本地文件\",\n"
         "             \"查看本地目录\", \"访问网页\")\n")

C = CASE + "::"
# 点名：只看失败条数说明不了失败的是不是那一条
ANCHORS: list[tuple[str, str, str, list]] = [
    # A：未知工具名退回英文 ValueError —— 被压成"请求内容有误"
    ("A 未知工具名退回英文异常", KNOWN + RAISE,
     '    raise ValueError(f"unknown system tool: {name}")',
     [C + "test_unknown_tool_names_what_and_options",
      C + "test_text_is_chinese_without_internal_tokens"]),
    # B：不给出可选项 —— 只说"没有这个工具"，使用者无从改正
    ("B 未知工具名不给可选项", KNOWN + RAISE,
     '    raise UserError(f"没有名为「{name}」的工具")',
     [C + "test_unknown_tool_names_what_and_options"]),
    # C：撤掉参数格式校验 —— 崩在取字段时，落到"操作失败"
    ("C 撤掉参数格式校验", GUARD, "",
     [C + "test_non_dict_args_reports_format"]),
]


def _write(path: Path, text: str) -> bool:
    for _ in range(6):
        tmp = Path(str(path) + ".revtmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        if path.read_text(encoding="utf-8") == text:
            return True
        time.sleep(0.15)
    return False


def _run() -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数说明不了失败的是不是预期那一条。
    """
    return pytest_run(CASE)


def _failed(out: str) -> int:
    m = re.search(r"(\d+) failed", out)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+) error", out)
    return int(m.group(1)) if m else 0


def main() -> int:
    ok = True
    orig = SRC.read_text(encoding="utf-8")
    for name, old, new, expect in ANCHORS:
        print(f"--- {name} ---")
        if old not in orig:
            print("  注入未命中，锚点无效\n")
            ok = False
            continue
        patched = orig.replace(old, new, 1)
        try:
            ast.parse(patched)
        except SyntaxError as exc:
            print(f"  [注入后语法损坏，放弃] {exc}\n")
            ok = False
            continue
        if not _write(SRC, patched):
            print("  [写入未落盘，放弃]\n")
            ok = False
            continue
        try:
            assert new in SRC.read_text(encoding="utf-8") or not new, "注入未生效"
            code, failed, out = _run()
            caught, why = verdict(code, failed, expect)
            print(f"  退出码 {code} → {'抓到' if caught else '未抓到'}"
                  f"（{why}）")
            if not caught:
                ok = False
        finally:
            # 还原失败必须被看见：原子替换仍可能不落盘，那样源码会停在
            # 已注入状态，后续所有校验都在脏代码上跑，且 git 工作区看着
            # 像真实回归。文件都在版本控制内，HEAD 就是可信的干净版本。
            if not _write(SRC, orig):
                subprocess.run(["git", "checkout", "--", str(SRC)],
                               cwd=str(ROOT), capture_output=True)
            if SRC.read_text(encoding="utf-8") != orig:
                print("  [还原失败：源码停在已注入状态，后续结论不可信]")
                ok = False

    code, _f, out = _run()
    print(f"\n--- 还原后 --- 退出码 {code}")
    if code != 0:
        print(out[-1200:])
        ok = False
    print("结论：", "全部锚点抓到" if ok else "存在未抓到的锚点")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
