#!/usr/bin/env python3
"""前端中文化标签守卫的判定校验。

## 七个校验点

 * A 撤掉 check_labels 的缺字段判定 → 1 条
 * B 删掉 created_ts 的标签 → 3 条
 * C 标签表加回 created_at（漂移回潮）→ 1 条
 * D _STALE_KEYS 放进 created_ts（防修过头）→ 1 条
 * E 清空报告字段快照 → 1 条
 * F 页面改读 created_at → 1 条
 * G 基线

## A 为什么必须单独验"能报错"

`test_check_labels_script_passes` 只断言脚本退出码 0。撤掉脚本里"有缺字段
就退出码 1"的判定后，退出码照样是 0——脚本从此查不出任何缺失，而守卫
依然全绿。这是所有"只验脚本通过"的守卫共有的洞：通过只能说明脚本没报错，
说明不了它还在查。

补的 `test_check_labels_script_can_fail` 把标签表指向删掉一个真实字段的
副本，退出码必须为 1——验的是"缺字段那一侧真的会响"。

## C 与 D 是同一条断言的两个方向

C 让漂移字段名回潮，D 把真实字段名也列进禁用清单。两者都只红
`test_stale_field_names_absent` 一条，缺任何一个方向都只能证明一半：
C 证明"回潮会被抓"，D 证明"清单没有被放大到连真实字段一起禁"。

## 还原为什么不能只做 move

备份用 copy2、还原用 move，**两者都保留 mtime**；而 Python 判断是否复用
`.pyc` 只看源码 mtime + 大小。本文件的 A 锚点（`return 1` → `return 0`）
长度不变，还原后缓存记录的 mtime 与大小仍吻合——后续跑用例执行的仍是注入
期间那份字节码。表现是"源码看着已还原、基线却报全绿"，而绿的是注入版的
行为。故用 `restore_src`（额外刷新 mtime 并删对应缓存），并在每次跑用例
前清缓存。常驻守卫见 `tests/test_rev_verdict.py`。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import (  # noqa: E402
    pytest_run, restore_src, verdict)

TESTS = "tests/test_frontend_labels_coverage.py"
C = TESTS + "::"
LAB = ROOT / "frontend" / "src" / "lib" / "labels.ts"
PAGE = ROOT / "frontend" / "src" / "pages" / "RunsPage.tsx"
CHK = ROOT / "scripts" / "check_labels.py"
TF = ROOT / TESTS

CAN_FAIL = C + "test_check_labels_script_can_fail"
PASSES = C + "test_check_labels_script_passes"
CREATED = C + "test_created_ts_labeled_and_used_consistently"
GENOME = C + "test_genome_snapshot_keys_are_labeled"
STALE = C + "test_stale_field_names_absent"
REPORT = C + "test_report_snapshot_keys_are_labeled"

STALE_LINE = '_STALE_KEYS = ("created_at", "phases")'
CREATED_TS_LABEL = "  created_ts: '创建时间',\n"


def _snapshot_block() -> str:
    """报告快照整块：整块替换才能真清空，只改首行内容还在。"""
    src = TF.read_text(encoding="utf-8")
    m = re.search(r"_REPORT_KEYS_SNAPSHOT = \{.*?\n\}", src, re.S)
    if not m:
        raise SystemExit("锚点失效：找不到报告字段快照块")
    return m.group(0)


ANCHORS = [
    ("A 撤掉 check_labels 的缺字段判定",
     [(CHK, "        return 1", "        return 0")], [CAN_FAIL]),
    ("B 删掉 created_ts 的标签",
     [(LAB, CREATED_TS_LABEL, "")], [PASSES, CREATED, GENOME]),
    ("C 标签表加回 created_at（漂移回潮）",
     [(LAB, "  schema_version: '结构版本',",
       "  schema_version: '结构版本',\n  created_at: '创建时间',")], [STALE]),
    ("D _STALE_KEYS 放进 created_ts（防修过头）",
     [(TF, STALE_LINE, '_STALE_KEYS = ("created_at", "phases", "created_ts")')],
     [STALE]),
    ("E 清空报告字段快照",
     [(TF, _snapshot_block(), "_REPORT_KEYS_SNAPSHOT = set()")], [REPORT]),
    ("F 页面改读 created_at",
     [(PAGE, "fmtTime(r.created_ts)", "fmtTime(r.created_at)")], [CREATED]),
    ("G 基线", [], []),
]

FILES = [LAB, PAGE, CHK, TF]


def self_heal() -> bool:
    """清掉遗留的注入与备份，返回是否可以继续。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。

    遇未提交改动**一律中止，不自动还原**：HEAD 只代表"上一次提交"，
    拿它覆盖当前工作区会把刚写好的用例一并抹掉（注入残留与正常改动在
    `git diff` 里长得一样，脚本无从分辨）。所以这里只做安全的那半——
    把遗留的 .bak 还原回去，其余交给人判断。
    """
    for path in FILES:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            restore_src(path, bak)
            print(f"  [自愈] 还原 {path.name}")
    dirty = subprocess.run(["git", "diff", "--name-only", "--"] +
                           [str(p.relative_to(ROOT)) for p in FILES],
                           cwd=str(ROOT), capture_output=True, text=True)
    names = (dirty.stdout or "").split()
    if names:
        print("  [中止] 这些受守护文件有未提交改动，无法确定是不是注入残留：")
        for n in names:
            print(f"      {n}")
        print("      请先提交或手工还原，再跑本脚本。")
        return False
    return True


def main() -> int:
    if not self_heal():
        return 2
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
                if not bak.exists():
                    shutil.copy2(path, bak)
                baks.append((path, bak))
            for path, old, new in edits:
                src = path.read_text(encoding="utf-8")
                path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed, tail = pytest_run(TESTS)
        finally:
            for path, bak in baks:
                if bak.exists():
                    restore_src(path, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:56]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    ran = len([a for a in ANCHORS if not want or a[0][0] in want])
    print(f"本段校验点 {ran} 个，抓到 {ran - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
