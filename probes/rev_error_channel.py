#!/usr/bin/env python3
"""未预期异常输出通道守卫的回退校验。

注入回退后先做语法校验：语法坏掉的文件会让测试直接崩，"崩"与"抓到"在
输出里都表现为失败，不校验会把假结果当成证据。

写入走临时文件 + 原子替换，并把"读回等于写入内容"当作成功判据：原地
改写同一文件时，写入调用返回成功后读回仍可能是旧内容，那样"注入生效"
的确认会失真。

用法：
    python3 probes/rev_error_channel.py
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
CASE = "tests/test_unexpected_error_channel.py"
C = CASE + "::"
CLI = ROOT / "omegaforge" / "cli.py"
SIDECAR = ROOT / "scripts" / "sidecar_main.py"

CLI_BRANCH = """    except Exception as e:
        # 只记日志、不回显异常原文：原文可能是英文且带本机路径。
        # 日志文件名可以告知——它是使用者自己数据目录下的文件，
        # 便于反馈问题时一并给出。
        record_internal_error(e, "cli.main")
        return _fail("程序遇到未预期的错误，详细信息已记入数据目录下的"
                     "日志文件 errors.log")
"""
CLI_NO_LOG = CLI_BRANCH.replace('        record_internal_error(e, "cli.main")\n', "")
SIDECAR_BRANCH = """    except Exception as exc:
        # 未预期的编程错误：完整调用栈只进内部日志，标准错误输出只留一句
        # 中文。否则英文调用栈连同本机路径会经外壳转发进他人可见的日志。
        from omegaforge.core.errors import record_internal_error

        record_internal_error(exc, "sidecar.main")
        print("[sidecar] 服务遇到未预期的错误，详细信息已记入数据目录下的"
              "日志文件 errors.log", flush=True)
        return 1
"""

# 点名：只看失败条数说明不了失败的是不是预期那一条
ANCHORS: list[tuple[str, Path, str, str, list]] = [
    ("A 命令行退回裸抛（英文栈直出）", CLI, CLI_BRANCH, "",
     [C + "test_no_absolute_path_in_user_channel",
      C + "test_stack_is_recorded_for_diagnosis",
      C + "test_user_channel_is_chinese_without_traceback"]),
    ("B 命令行只改文案不落盘", CLI, CLI_BRANCH, CLI_NO_LOG,
     [C + "test_stack_is_recorded_for_diagnosis"]),
    ("C 后台进程退回裸抛", SIDECAR, SIDECAR_BRANCH, "",
     [C + "test_sidecar_channel_is_clean_and_logged"]),
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
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。"""
    return pytest_run(CASE)


def main() -> int:
    ok = True
    for name, path, old, new, expect in ANCHORS:
        print(f"--- {name} ---")
        orig = path.read_text(encoding="utf-8")
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
        if not _write(path, patched):
            print("  [写入未落盘，放弃]\n")
            ok = False
            continue
        try:
            code, failed, out = _run()
            caught, why = verdict(code, failed, expect)
            print(f"  退出码 {code} → {'抓到' if caught else '未抓到'}"
                  f"（{why}）")
            if not caught:
                ok = False
        finally:
            if not _write(path, orig):
                subprocess.run(["git", "checkout", "--", str(path)],
                               cwd=str(ROOT), capture_output=True)
            if path.read_text(encoding="utf-8") != orig:
                print("  [还原失败：源码停在已注入状态，后续结论不可信]")
                ok = False

    code, _f, out = _run()
    print(f"\n--- 还原后 --- 退出码 {code}")
    if code != 0:
        print(out[-1000:])
        ok = False
    print("结论：", "全部锚点抓到" if ok else "存在未抓到的锚点")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
