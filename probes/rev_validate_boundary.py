#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验：core/validate.py 三处修复的守卫必须真能抓到回归。

判据是"注入后测试**以 failed 结束**"，不是"rc≠0"。
（原先踩过：注入把文件改出语法错误，测试根本没跑起来，rc≠0 被误读成
"抓到"。所以每次注入后强制 ast.parse 校验语法。）

校验点
----
A  把 as_int str 分支的 `if not math.isfinite(val)` 加回去  -> 必须红
   （这正是 400 位数字串变 500 的成因）
B  撤掉 require 的空容器判定                                 -> 必须红
C  撤掉 pick_first 对 bool / 容器的跳过                      -> 必须红
D  让 pick_first 连数字一起跳过（过度收紧）                  -> 必须红
   （防修过头：数字 id 必须仍可取到）
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "omegaforge" / "core" / "validate.py"
GUARD = "tests/test_validate_boundary.py"

C = GUARD + "::"

# 点名：只看失败条数说明不了失败的是不是预期那一条
INJECTIONS = {
    "A 加回 as_int 的 math.isfinite": (
        """        try:
            val = int(s)
        except ValueError:
            raise UserError(f"{name}需要填写数字，当前填写的不是有效数值")
""",
        """        try:
            val = int(s)
        except ValueError:
            raise UserError(f"{name}需要填写数字，当前填写的不是有效数值")
        if not math.isfinite(val):
            raise UserError(f"{name}需要填写有效数字")
""",
    [C + "test_wide_digit_string_message_names_the_field",
                                        C + "test_wide_digit_string_never_raises_non_usererror",
                                        C + "test_wide_priority_is_400_not_500"],
    ),
    "B 撤掉 require 空容器判定": (
        """        if isinstance(v, (list, tuple, dict, set)) and not v:
            return True
""",
        "",
    [C + "test_empty_container_counts_as_missing"],
    ),
    "C 撤掉 pick_first 对 bool/容器的跳过": (
        """        if isinstance(v, bool):
            continue
        if isinstance(v, (dict, list, tuple, set)):
            continue
""",
        "",
    [C + "test_bool_is_not_text",
                                        C + "test_container_is_not_text",
                                        C + "test_skipping_continues_to_next_key"],
    ),
    "D pick_first 连数字一起跳过（过度收紧）": (
        "        if isinstance(v, (dict, list, tuple, set)):\n            continue\n",
        "        if isinstance(v, (dict, list, tuple, set, int, float)):\n            continue\n",
    [C + "test_number_still_usable"],
    ),
}


def run() -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数说明不了失败的是不是预期那一条。
    """
    return pytest_run(GUARD)


def failed_count(out: str) -> int:
    m = re.findall(r"(\d+) failed", out)
    return int(m[-1]) if m else 0


def main() -> int:
    print("=" * 72)
    print("第 46 章反向验证：core/validate.py 三处修复")
    print("=" * 72)

    rc, _f, out = run()
    print(f"\n[基线] rc={rc} failed={failed_count(out)}")
    if rc != 0:
        print("基线就不干净，无法反向验证")
        return 1

    orig = TARGET.read_text(encoding="utf-8")
    results = {}
    try:
        for name, (old, new, expect) in INJECTIONS.items():
            if old not in orig:
                print(f"\n[{name}] ✗ 锚点失效：注入点未找到（锚点未执行，\n                  不是\"跑了没抓到\"，必须重定位）")
                results[name] = False
                continue
            patched = orig.replace(old, new, 1)
            try:
                ast.parse(patched)          # 语法必须仍然合法
            except SyntaxError as e:
                print(f"\n[{name}] 注入后语法错误: {e} —— 记为未抓到")
                results[name] = False
                continue
            TARGET.write_text(patched, encoding="utf-8")
            rc, failed, out = run()
            ok, why = verdict(rc, failed, expect)
            print(f"\n[{name}] rc={rc} {'抓到' if ok else '未抓到'}（{why}）")
            results[name] = ok
    finally:
        TARGET.write_text(orig, encoding="utf-8")
        if TARGET.read_text(encoding="utf-8") != orig:
            print("[还原失败：源码停在已注入状态，后续结论不可信]")

    print("\n" + "=" * 72)
    print(f"反向验证: {sum(results.values())}/{len(results)} 锚点抓到")
    print("=" * 72)
    for k, v in results.items():
        print(f"  {'抓到  ' if v else '未抓到'} {k}")

    rc, _f, out = run()
    print(f"\n[还原校验] rc={rc} failed={failed_count(out)}")
    return 0 if all(results.values()) and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
