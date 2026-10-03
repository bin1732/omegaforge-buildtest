#!/usr/bin/env python3
"""全量回归入口的判定校验。

入口的作用是把"环境没配好"与"规模大跑不完"分开：前者补上搜索路径即可，
后者才需要拆分。它对空收集与用例失败分别判失败；两条判定任一失效，入口
会恒返回 0——届时"全量回归通过"这句话将不再有任何依据。

每个校验点：

  A 撤掉"收集为 0 判失败" → 空目录也返回 0（抓到）
  B 撤掉"有失败判失败"   → 失败用例也返回 0（抓到）
  C 基线                 → 正常用例返回 0（抓到）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "probes" / "run_full_regression.py"
TEMP_FAIL = ROOT / "tests" / "_rev_temp_fail.py"

ANCHORS = [
    ("A 撤掉收集为0判失败", ENTRY,
     '        print("未通过：没收集到任何用例，目录或文件名可能已变更")\n'
     '        print((c.stdout + c.stderr).strip()[-400:])\n'
     '        return 1',
     '        print("未通过：没收集到任何用例，目录或文件名可能已变更")\n'
     '        print((c.stdout + c.stderr).strip()[-400:])\n'
     '        return 0',
     "tests_no_such", 1),
    ("B 撤掉有失败判失败", ENTRY,
     "    if p.returncode != 0:",
     "    if False:",
     "tests/_rev_temp_fail.py", 1),
    ("C 基线", ENTRY, None, None, "tests/test_import_purity.py", 0),
]


def self_heal():
    """清掉遗留的注入痕迹与临时用例。

    临时用例若留在 tests/ 里，全量回归会被它染红，而染红的原因与产品无关
    ——排查方向会被带偏。因此开局无条件清理。
    """
    if TEMP_FAIL.exists():
        TEMP_FAIL.unlink()
        print("  [自愈] 删除遗留临时用例")
    bak = Path(str(ENTRY) + ".bak")
    if bak.exists():
        shutil.move(str(bak), str(ENTRY))
        print("  [自愈] 还原入口脚本")


def write_temp_fail():
    TEMP_FAIL.write_text(
        "# 反向验证临时用例：不参与产品回归，仅用于确认入口会对失败判失败。\n"
        "def test_rev_temp_always_fails():\n"
        "    assert False\n", encoding="utf-8")


def run_entry(target: str) -> int:
    p = subprocess.run(
        [sys.executable, str(ENTRY), "--dir", target],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    return p.returncode


def main() -> int:
    self_heal()
    bad = []
    for name, path, old, new, target, expect_rc in ANCHORS:
        restored = False
        if old is not None:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次")
                bad.append(name)
                continue
            shutil.copy2(path, str(path) + ".bak")
            path.write_text(src.replace(old, new, 1), encoding="utf-8")
            restored = True
        try:
            if name.startswith("B"):
                write_temp_fail()
            rc = run_entry(target)
        finally:
            if name.startswith("B") and TEMP_FAIL.exists():
                TEMP_FAIL.unlink()
            if restored:
                shutil.move(str(path) + ".bak", str(path))
        # 注入后退出码应当偏离正常值；基线应当为 0。
        want = 0 if old is None else (0 if expect_rc else 1)
        ok = (rc == want)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（rc={rc}，期望{want}）")
        if not ok:
            bad.append(name)

    print("\n=== 汇总 ===")
    print(f"校验点 {len(ANCHORS)} 个，抓到 {len(ANCHORS) - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
