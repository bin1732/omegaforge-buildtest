#!/usr/bin/env python3
"""技能安装与边界标记守卫的判定校验。

## 九个校验点

 * A 撤掉 name 的 neutralize → 2 条
 * B 撤掉 description 的 neutralize → 1 条
 * C 撤掉符号链接检查 → 3 条
 * D 撤掉 install 的体积预检 → 2 条
 * E 撤掉 list 的体积预检 → 1 条
 * F 撤掉 tmp 目录名的随机串 → 1 条
 * G 符号链接恒拒（防修过头）→ 5 条
 * H name 恒置空（防修过头）→ 1 条
 * I 基线

## F 为什么必须绕过锁才验得到

install() 全程持 file_lock，串行化之后两个写入者根本不会同时进入 tmp
阶段。于是撤掉 tmp 名里的随机串后，走 install 的用例一条都不红——锁把
这条纵深防御整个盖住了。要验它必须绕过锁。

锁挡的是 TOCTOU，命名唯一挡的是"同一进程内两个写入者共用同一目录"，
两者不互相替代，但只有绕过锁才验得到后者。故用例直接调
`_install_locked`，并把断言落在**观测到的 tmp 名种类数**上：失败与否还
受 rename 那段 TOCTOU 影响，两种注入都会失败，区分不出命名唯一这一维。

## D 与 E 是两条独立路径

D 落在 install，E 落在 list。二者不是同一处代码的两个入口，而是两个
不同落点：list 与 `/api/skills` 同源、界面打开即调用，坏技能会把技能页
自己拖垮——而技能页正是卸载坏技能的**唯一入口**。只验 D 的话，E 被摘掉
不会有任何校验点变红。

## G / H 为什么是反向的

只验"撤掉修复会变红"证明不了修复刚好够用。G 让任何技能包都被当成含
符号链接而拒收，H 把技能名一律置空：攻击侧用例一条都不会红，只有"正常
包必须照常装上"那组会红。这类失效不会被任何"撤掉修复"的锚点抓到。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。同一文件多处替换时只备份一次，否则后一次备份覆盖
前一次，还原回来的是已注入的版本。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_selfheal import (
    dirty_names,
    report_and_stop,
)
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_skill_install_boundary.py"
C = TESTS + "::"
PROV = ROOT / "omegaforge" / "tools" / "provenance.py"
MGR = ROOT / "omegaforge" / "skills" / "manager.py"

NAME_N = ('    name = neutralize(name) or "skill"')
NAME_RAW = ('    name = name or "skill"')
NAME_EMPTY = ('    name = ""')

DESC_N = ("    desc = neutralize(description)")
DESC_RAW = ("    desc = description")

LINK = ("        link = _find_symlink(source_dir)")
LINK_NONE = ("        link = None")
LINK_ALL = ('        link = "x"')

PRE_INST = ("        _precheck_size(src_md)\n        # 符号链接必须拒")
PRE_INST_OFF = ("        pass\n        # 符号链接必须拒")

PRE_LIST = ("                _precheck_size(md)\n                with open(md")
PRE_LIST_OFF = ("                pass\n                with open(md")

TMP = ('f".tmp-{name}-{os.getpid()}-{uuid.uuid4().hex[:8]}")')
TMP_FIXED = ('f".tmp-{name}-{os.getpid()}")')

MARKER_ONCE = C + "test_end_marker_appears_once"
MARKER_LEAK = C + "test_no_content_leaks_after_first_end"
DESC_T = C + "test_description_also_neutralized"
SYM_FILE = C + "test_file_symlink_rejected"
SYM_DIR = C + "test_dir_symlink_rejected"
SYM_NESTED = C + "test_nested_symlink_rejected"
CAP_T = C + "test_bytes_cap_still_blocks_huge_file"
REAL_BIG = C + "test_install_real_big_file_not_read"
LIST_T = C + "test_list_does_not_read_big_file"
TMP_T = C + "test_tmp_dir_names_are_unique_per_writer"
CLEAN_PKG = C + "test_no_over_fix_clean_package_installs"
CONC_T = C + "test_no_spurious_failure"
CN_T = C + "test_chinese_body_near_char_limit_accepted"
INVOKE_T = C + "test_invoke_rejects_oversized"
CLEAN_T = C + "test_clean_skill_unaffected"

ANCHORS = [
    ("A 撤掉 name 的 neutralize", [(PROV, NAME_N, NAME_RAW)],
     [MARKER_ONCE, MARKER_LEAK]),
    ("B 撤掉 description 的 neutralize", [(PROV, DESC_N, DESC_RAW)], [DESC_T]),
    ("C 撤掉符号链接检查", [(MGR, LINK, LINK_NONE)],
     [SYM_FILE, SYM_DIR, SYM_NESTED]),
    ("D 撤掉 install 的体积预检", [(MGR, PRE_INST, PRE_INST_OFF)],
     [CAP_T, REAL_BIG]),
    ("E 撤掉 list 的体积预检", [(MGR, PRE_LIST, PRE_LIST_OFF)], [LIST_T]),
    ("F 撤掉 tmp 目录名的随机串", [(MGR, TMP, TMP_FIXED)], [TMP_T]),
    ("G 符号链接恒拒（防修过头）", [(MGR, LINK, LINK_ALL)],
     [CLEAN_PKG, CONC_T, CN_T, INVOKE_T, LIST_T]),
    ("H name 恒置空（防修过头）", [(PROV, NAME_N, NAME_EMPTY)], [CLEAN_T]),
    ("I 基线", [], []),
]

FILES = [PROV, MGR]


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

    print("\n=== 汇总 ===")
    ran = len([a for a in ANCHORS if not want or a[0][0] in want])
    print(f"本段校验点 {ran} 个，抓到 {ran - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
