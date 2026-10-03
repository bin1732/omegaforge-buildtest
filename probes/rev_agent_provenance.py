#!/usr/bin/env python3
"""工具输出降级为"数据"的守卫的判定校验。

## 十个校验点

 * A `_tool_turn` 不再调用 `_collapse_wrapped` → 2 条
 * B `_collapse_wrapped` 恒返回原值 → 3 条
 * C run 循环不再用 `_tool_turn` → 1 条
 * D run 循环不再保留 assistant 轮 → 1 条
 * E 退回单轮（只送最后一条）→ 1 条
 * F 截断挪到包裹之后 → 1 条
 * G `wrap` 不化解伪造标记 → 1 条
 * H `taint` 恒标可疑（防修过头）→ 1 条
 * I `wrap` 清空正文（防修过头）→ 3 条
 * J 基线

## 为什么 A 必须存在：两层互相盖住，只验嵌套那一维区分不出来

"结束标记恰好一个"由两层共同保证：`_collapse_wrapped` 去掉内层包裹版、
`neutralize` 化解内容自带的标记。撤掉任一层，另一层仍把嵌套挡住——只看
`_END` 的次数，两层都在与只剩一层给出同一个数。

两层各自有一维是对方盖不住的，故各配一条：

 · `test_foreign_text_appears_once`：只剩 neutralize 时，原文在 text 与
   text_wrapped 里各出现一次。重复翻倍 token，且在同样长度的块里把
   "这是被引用的数据"的信号稀释掉一半。
 · `test_no_forged_credibility_declaration`：只剩 neutralize 时，内容自带
   的 `source=... trusted=no` 那行**照旧留在块内**（被替掉的只是标记记号
   本身）。模型读到两条互相矛盾的来源声明，其中一条是外部内容自己写的。

## C 为什么不能省

`_tool_turn` 存在但 run 循环不调用它，包一层的功夫全白费。这是"测了内层
没测接通"那一类；只测 `_tool_turn` 时 C 的注入不会让任何用例变红。

## D 与 E 原本落在同一条用例上

两条断言（任务仍在上下文 / assistant 轮仍在上下文）原先写在同一个方法里，
于是 D 与 E 各让同一条变红，区分不出是哪一个。已拆成两条，各自可定位。
D 撤掉的是"模型自己说过什么必须留下"，E 撤掉的是"原始任务不得消失"——
后者是历史上真发生过的 bug（每轮重开单轮 chat）。

## H / I 为什么是反向的

只验"撤掉修复会变红"证明不了修复刚好够用。H 让任何输出都被标成可疑——
标记泛滥等于没有标记；I 把正文整个清空——那是过滤，不是降级，业务语义
全丢。这两种写法下攻击侧用例一条都不会红。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。同一文件多处替换时只备份一次。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_selfheal import (          # noqa: E402
    dirty_names,
    report_and_stop,
)
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_agent_provenance.py"
C = TESTS + "::"
SA = ROOT / "omegaforge" / "agent" / "super_agent.py"
PV = ROOT / "omegaforge" / "tools" / "provenance.py"

# --- 源文件里的锚点（原文 → 注入版） -------------------------------
COLLAPSE_CALL = ('        s = str(_collapse_wrapped(tool_out))')
COLLAPSE_CALL_OFF = ('        s = str(tool_out)')

COLLAPSE_POP = ('    collapsed.pop("text_wrapped", None)\n    return collapsed')
COLLAPSE_POP_OFF = ('    return tool_out')

RUN_WRAP = ('"content": _tool_turn(tname, tool_out)})')
RUN_WRAP_OFF = ('"content": str(tool_out)})')

ASSIST_KEEP = ('                convo.append({"role": "assistant", '
               '"content": r.text})')
ASSIST_KEEP_OFF = ('                pass')

FULL_CONVO = ('convo, model=self.llm.model_fast')
FULL_CONVO_OFF = ('convo[:1] + [convo[-1]], model=self.llm.model_fast')

TRUNC_FIRST = ('        return "TOOL " + tool_name + " -> " + taint(\n'
               '            s, f"tool:{tool_name}")["text"]')
TRUNC_LAST = ('        return ("TOOL " + tool_name + " -> " + taint(\n'
              '            s, f"tool:{tool_name}")["text"])[:_OUT_MAX]')

# `wrap` 与 `wrap_candidate` 上方那段防伪造注释**逐字相同**，只取注释会
# 命中 2 次。带上各自紧跟其后的 flag 行才能唯一（后者的拼接串不同）。
NEUT = ('    # 防伪造必须发生在包裹**之前**：否则内容自带的结束标记会提前\n'
        '    # 关闭分隔标记（残留 63 字符逃逸到块外）。\n'
        '    body = neutralize(text)\n'
        '    flag = ""\n'
        '    if tags:\n'
        '        flag = f"\\n[可疑句式标记: ')
NEUT_OFF = ('    body = text\n'
            '    flag = ""\n'
            '    if tags:\n'
            '        flag = f"\\n[可疑句式标记: ')
NEUT_EMPTY = ('    body = ""\n'
              '    flag = ""\n'
              '    if tags:\n'
              '        flag = f"\\n[可疑句式标记: ')

TAINT_SCAN = ('    tags = scan(text)\n    return {')
TAINT_ALL = ('    tags = ["instruction_override"]\n    return {')

# --- 期望变红的用例 ------------------------------------------------
ONCE = C + "TestBoundaryUniquenessLayers::test_foreign_text_appears_once"
CRED = C + ("TestBoundaryUniquenessLayers::"
            "test_no_forged_credibility_declaration")
FORGED = C + "TestBoundaryUniquenessLayers::test_forged_end_marker_defused"
TRUNC_T = C + "TestBoundaryUniquenessLayers::test_truncation_keeps_end_marker"
INNER = C + "TestNoNestedBoundary::test_collapse_removes_inner_field"
WIRED = C + "TestRunLoopUsesWrapped::test_tool_output_reaches_model_wrapped"
TASK = C + "TestRunLoopUsesWrapped::test_task_stays_in_context"
ASSIST = C + "TestRunLoopUsesWrapped::test_assistant_output_stays_in_context"
BENIGN_MARK = C + "TestToolTurnDemotesToData::test_benign_not_marked_suspicious"
BENIGN_KEEP = C + ("TestToolTurnDemotesToData::"
                   "test_benign_content_preserved")
RAW_IN = C + "TestNoNestedBoundary::test_raw_content_stays_inside"
BENIGN_REACH = C + ("TestRunLoopUsesWrapped::"
                    "test_benign_output_still_reaches_model")

ANCHORS = [
    ("A _tool_turn 不调用 collapse", [(SA, COLLAPSE_CALL, COLLAPSE_CALL_OFF)],
     [ONCE, CRED]),
    ("B collapse 恒返回原值", [(SA, COLLAPSE_POP, COLLAPSE_POP_OFF)],
     [INNER, ONCE, CRED]),
    ("C run 循环不用 _tool_turn", [(SA, RUN_WRAP, RUN_WRAP_OFF)], [WIRED]),
    ("D 不保留 assistant 轮", [(SA, ASSIST_KEEP, ASSIST_KEEP_OFF)], [ASSIST]),
    ("E 退回单轮", [(SA, FULL_CONVO, FULL_CONVO_OFF)], [TASK, ASSIST]),
    ("F 截断挪到包裹之后", [(SA, TRUNC_FIRST, TRUNC_LAST)], [TRUNC_T]),
    ("G wrap 不化解伪造标记", [(PV, NEUT, NEUT_OFF)], [FORGED]),
    ("H taint 恒标可疑（防修过头）", [(PV, TAINT_SCAN, TAINT_ALL)],
     [BENIGN_MARK]),
    ("I wrap 清空正文（防修过头）", [(PV, NEUT, NEUT_EMPTY)],
     [RAW_IN, BENIGN_KEEP, BENIGN_REACH, ONCE]),
    ("J 基线", [], []),
]

FILES = [SA, PV]


def _restore(path: Path, bak: Path):
    shutil.move(str(bak), str(path))


def self_heal() -> bool:
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。

    但"残留"与"编写中的改动"在差异清单里完全同形，一律用 HEAD 覆盖会把
    当次正在写的源码一并抹掉。故这里只还原本脚本留下的 .bak，仍有差异时
    中止并交给人判断。
    """
    for path in FILES:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            _restore(path, bak)
            print(f"  [自愈] 还原 {path.name}")
    names = dirty_names(FILES, ROOT)
    if names:
        return report_and_stop(names)
    return True


def main() -> int:
    if not self_heal():
        return 1
    want = sys.argv[1:]
    bad = []
    for name, edits, expect in ANCHORS:
        if want and name[0] not in want:
            continue
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        missing = False
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次，"
                      f"须重定位（{path.name}）")
                bad.append(name)
                missing = True
                break
            if path not in paths:
                paths.append(path)
        if missing:
            continue

        baks = []
        try:
            for path in paths:
                bak = Path(str(path) + ".bak")
                shutil.copy2(path, bak)
                baks.append((path, bak))
            for path, old, new in edits:
                src = path.read_text(encoding="utf-8")
                path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed, tail = pytest_run(TESTS)
        finally:
            for path, bak in baks:
                if bak.exists():
                    _restore(path, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:56]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    if bad:
        print(f"\n未抓到：{bad}")
        return 1
    print("\n全部校验点抓到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
