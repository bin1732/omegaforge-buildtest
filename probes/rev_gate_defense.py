#!/usr/bin/env python3
"""门禁防御守卫的判定校验。

## 七个锚点：四个撤防线 + 三个防修过头

 * A 撤掉管道喂解释器判定：`curl x | sh` 整条放行 → 5 条变红。
 * B 撤掉段首词判定：`su -` / `su root` 放行 → 2 条变红。
 * C 撤掉聚合检查：四步攻击链在完全访问模式下走通 → 1 条变红。
 * D 管道判定改成拦所有管道：`ls -la | grep su` 被误杀 → 4 条变红。
 * E 聚合改成恒拦截：正常脚本也执行不了 → 2 条变红。
 * F 目标提取不认执行程序名：`bash combined.sh` 查不到引用 → 2 条变红。
 * G 目标提取不认 `./` 直接执行：`./run.sh` 查不到引用 → 1 条变红。

## A 与 B 为什么分开

两层认的东西不重叠：管道判定认的是"管道右侧的程序名是不是解释器"，
段首词判定认的是"这一段开头的词是不是提权命令"。`sudo ls` 由整词清单独
自拦住，段首词判定只覆盖 `su`，所以撤掉段首词只红 2 条而非 3 条——
这两条正是没有别的层能拦住的部分。合并成一个校验点会把两层的失效
混成"有红"，看不出是哪一层没了。

## 为什么要有防修过头锚点

只验"撤掉修复会变红"，无法区分"修复刚好够用"与"拦截范围被放大"。
D 把管道判定改成拦任何含管道的命令，此时攻击类用例照样红，只有
benign 那组会红——缺了 D，把拦截放大到禁止一切管道同样能报全绿。
E 同理：聚合恒拦截时攻击链用例仍红，只有"正常脚本能跑"这条会红。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。同一文件多处替换时只备份一次，否则后一次备份
覆盖前一次，还原回来的是已注入的版本。
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

TESTS = "tests/test_gate_defense.py"
C = TESTS + "::"
ST = ROOT / "omegaforge" / "tools" / "system_tools.py"
LED = ROOT / "omegaforge" / "tools" / "ledger.py"

OLD_PIPE = ('    # 管道右侧喂解释器\n'
            '    segs = re.split(r"\\s*\\|\\s*", norm)\n'
            '    for seg in segs[1:]:\n'
            '        if _seg_head(seg) in _PIPE_INTERPRETERS:\n'
            '            return True\n')
NEW_PIPE_OFF = '    # 管道右侧喂解释器\n    return False\n'
NEW_PIPE_ALL = ('    # 管道右侧喂解释器\n'
                '    segs = re.split(r"\\s*\\|\\s*", norm)\n'
                '    if len(segs) > 1:\n'
                '        return True\n')

OLD_SU = ('    for seg in re.split(r"\\s*\\|\\s*", norm):\n'
          '        head = _seg_head(seg)\n'
          '        if head in ("su",):\n'
          '            return True\n')
NEW_SU = ('    for seg in re.split(r"\\s*\\|\\s*", norm):\n'
          '        head = _seg_head(seg)\n')

OLD_AGG = '        found = self._scan_script_chain(script_targets(cmd))'
NEW_AGG_OFF = '        found = None'
NEW_AGG_ALL = ('        found = self._scan_script_chain(script_targets(cmd))\n'
               '        if found is None:\n'
               '            raise BlockedCommand("聚合恒拦截探测")')

OLD_EXEC = '        if head in _EXEC_PROGS:'
NEW_EXEC = '        if False:'
OLD_DOT = '        elif seg.startswith("./") or seg.startswith(".\\\\"):'
NEW_DOT = '        elif False:'

PIPE = C + "test_pipe_to_interpreter_blocked"
BENIGN = C + "test_benign_commands_not_blocked"
PRIV = C + "test_privilege_commands_still_blocked"
SPLIT = C + "test_split_then_execute_blocked"
BENIGN_SCRIPT = C + "test_benign_script_still_runs"
TARGETS = C + "test_script_targets"

ANCHORS = [
    ("A 管道喂解释器判定失效", [(ST, OLD_PIPE, NEW_PIPE_OFF)], [PIPE]),
    ("B 段首词判定失效", [(ST, OLD_SU, NEW_SU)], [PRIV]),
    ("C 聚合检查失效", [(ST, OLD_AGG, NEW_AGG_OFF)], [SPLIT]),
    ("D 管道被一律拦下（防修过头）", [(ST, OLD_PIPE, NEW_PIPE_ALL)], [BENIGN]),
    ("E 聚合恒拦截（防修过头）", [(ST, OLD_AGG, NEW_AGG_ALL)],
     [BENIGN_SCRIPT, SPLIT]),
    ("F 目标提取不认执行程序名", [(LED, OLD_EXEC, NEW_EXEC)], [TARGETS, SPLIT]),
    ("G 目标提取不认 ./ 直接执行", [(LED, OLD_DOT, NEW_DOT)], [TARGETS]),
    ("H 基线", [], []),
]

FILES = [ST, LED]


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
    bad = []
    for name, edits, expect in ANCHORS:
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次，须重定位")
                bad.append(name)
                break
            if path not in paths:
                paths.append(path)
        else:
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
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:70]}）")
            if not ok:
                bad.append(name)
            rc2, _, tail2 = pytest_run(TESTS)
            if rc2 != 0:
                print(f"      还原后仍非全绿：{tail2}")
                bad.append(name + "（还原失败）")
            continue
        continue

    print("\n=== 汇总 ===")
    print(f"校验点 {len(ANCHORS)} 个，抓到 {len(ANCHORS) - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
