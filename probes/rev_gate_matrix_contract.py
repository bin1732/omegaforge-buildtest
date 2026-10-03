#!/usr/bin/env python3
"""门禁矩阵契约守卫的判定校验：撤掉修复，指定的用例必须变红。

## 为什么要点名到用例

只比对"有没有失败项"区分不了两件事：失败的是不是这一处守卫对应的那条
用例。给被测模块加一个模块级未定义名，整片用例都会失败，任何"有失败"
的判定都会被满足——那时的红是环境故障，不是守卫在起作用。

因此每个校验点都点名预期变红的用例，并且严格：出现预期之外的失败项同样
判未抓到。

## 两类校验点缺一不可

A~F 是"撤掉修复"方向，证明修复有用；G、H 是"防修过头"方向，证明修复
刚好够用。只留前一类的话，把档位收紧成一律询问照样报全绿——拦截确实
发生了，只是正常用法一起被拦。这类失效不会被任何"撤掉"方向的校验点
发现。

## 点名的来源

每个点名清单都要逐个注入确认。照着注入点推出来的清单会漏掉连带失败的
那几条，而漏掉的后果是：注入生效了，判定却说"预期之外还失败"。

用法::

    python3 probes/rev_gate_matrix_contract.py          # 全部校验点
    python3 probes/rev_gate_matrix_contract.py A B      # 只跑指定校验点
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT", "/data/workspace/LATEST"))
sys.path.insert(0, str(ROOT / "probes"))

from _rev_verdict import pytest_run, restore_src, verdict  # noqa: E402

POLICY = ROOT / "omegaforge" / "tools" / "policy.py"
TOOLS = ROOT / "omegaforge" / "tools" / "system_tools.py"
TESTS = "tests/test_gate_matrix_contract.py"
BAK = Path("/data/workspace/.rev_backups")
BAK.mkdir(parents=True, exist_ok=True)


def c(kls: str, name: str) -> str:
    return f"{TESTS}::{kls}::{name}"


_M = "MatrixTest"
_W = "McpPersonalWriteTest"
_P = "PlanModeTest"
_G = "McpApprovalGuidanceTest"
_N = "NameRegistrationTest"
_U = "GatePersonalWriteUnitTest"

# (标识, 说明, 目标文件, [(注入前, 注入后)], 预期变红用例)
ANCHORS = [
    ("A", "放行侧不留痕（只记拒绝不记放行）", POLICY, [
        ('        audit_write(dict(ev, verdict="allow", reason="personal_write"))\n',
         ""),
    ], [c(_W, "test_4_allow_is_audited")]),

    ("B", "ask 侧不留痕（越权尝试无从追溯）", POLICY, [
        ('    audit_write(dict(ev, verdict="ask"))\n', ""),
    ], [c(_U, "test_4_ask_is_audited_too")]),

    ("C", "风险表登记幽灵名（会被渲染进设置页）", POLICY, [
        ('    "wiki_save":       ("", "medium"),\n',
         '    "wiki_save":       ("", "medium"),\n'
         '    "wiki_put":        ("", "medium"),\n'),
    ], [c(_N, "test_2_no_phantom_names")]),

    ("D", "真实暴露的写工具没登记（表与实现分叉）", POLICY, [
        ('    "wiki_save":       ("", "medium"),\n', ""),
    ], [c(_N, "test_1_scope_table_is_a_subset_of_risk_table"),
        c(_N, "test_3_real_names_are_registered")]),

    ("E", "作用域表引用风险表里没有的工具", TOOLS, [
        ('                      "kb_add": False, "kb_delete": False,\n',
         '                      "kb_add": False, "kb_delete": False,\n'
         '                      "phantom_scope_tool": False,\n'),
    ], [c(_N, "test_1_scope_table_is_a_subset_of_risk_table")]),

    ("F", "个人写工具不在作用域表内（绕开作用域约束）", POLICY, [
        ('    "kb_add", "kb_delete", "memory_remember",\n',
         '    "kb_add", "kb_delete", "memory_remember",\n'
         '    "unscoped_personal_write",\n'),
    ], [c(_N, "test_5_personal_write_subset_of_scope_table")]),

    # -------- 防修过头方向 --------
    ("G", "full 档写工具改为询问（最宽档不再放行）", POLICY, [
        ('    "full":      {"low": "allow", "medium": "allow", "high": "allow"},\n',
         '    "full":      {"low": "allow", "medium": "ask", "high": "ask"},\n'),
    ], [c(_M, "test_1_declared_matrix_matches"),
        c(_M, "test_3_full_allows_writes"),
        c(_W, "test_3_full_still_writes"),
        c(_W, "test_4_allow_is_audited"),
        c(_U, "test_3_full_returns_allow")]),

    ("H", "plan 档改为询问（计划模式退化成等待确认）", POLICY, [
        ('    "plan":      {"low": "allow", "medium": "plan",  "high": "plan"},\n',
         '    "plan":      {"low": "allow", "medium": "ask",   "high": "ask"},\n'),
    ], [c(_M, "test_1_declared_matrix_matches"),
        c(_P, "test_1_plan_message_is_actionable"),
        c(_U, "test_2_plan_raises")]),

    # -------- 判定助手自身的自检 --------
    ("X", "无关故障不得被当成抓到（模块级未定义名）", POLICY, [
        ("from __future__ import annotations\n",
         "from __future__ import annotations\n_unrelated_undefined_name_\n"),
    ], []),
]


def main() -> int:
    only = set(sys.argv[1:])
    origs: dict[Path, str] = {p: p.read_text(encoding="utf-8")
                              for p in (POLICY, TOOLS)}

    caught = 0
    ran = 0
    for name, desc, target, subs, expect in ANCHORS:
        if only and name not in only:
            continue
        ran += 1
        cur = origs[target]
        hit = True
        for old, new in subs:
            if old not in cur:
                hit = False
                break
            cur = cur.replace(old, new, 1)
        if not hit:
            print(f"  [未抓到] {name} {desc} —— 注入点不存在，锚点须重定位")
            continue
        # 备份必须在注入之前落盘：若备份晚于注入，被中断后备份里存的是
        # 已注入版本，还原回到的是脏代码，此后每个校验点都在脏代码上跑。
        bak = BAK / (target.name + ".bak")
        bak.write_text(origs[target], encoding="utf-8")
        target.write_text(cur, encoding="utf-8")
        try:
            rc, failed, tail = pytest_run(TESTS)
        finally:
            restore_src(target, bak)
        got, why = verdict(rc, failed, expect)
        if name == "X":
            ok = not got
        else:
            ok = got
        caught += int(ok)
        print(f"  [{'抓到' if ok else '未抓到'}] {name} {desc}: {why}")

    print(f"\n{caught}/{ran} 抓到")

    rc, failed, tail = pytest_run(TESTS)
    print(f"  基线：{tail}")
    if rc != 0:
        print(f"  基线不通过：{failed[:5]}")
        return 1
    return 0 if caught == ran else 1


if __name__ == "__main__":
    sys.exit(main())
