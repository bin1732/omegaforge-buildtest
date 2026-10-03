#!/usr/bin/env python3
"""校验：说明文字整洁度扫描器的覆盖面。

只跑一次扫描看到"通过"不足以证明它有用——要证明的是：把问题放回去，
它确实会报出来。四个校验点分别对应一种会让它静默失效的情况：

  A 目录盲区：probes/ 里的作业记录（这两个目录原先不在扫描范围内）
  B 目录盲区：scripts/ 里的作业记录
  C 词形盲区：斜杠列举式章节引用（旧模式只认单个数字，匹配不到）
  D 接线：把 probes/ scripts/ 从扫描范围里去掉，同类问题应查不到

用法：python3 probes/rev_comment_hygiene_scope.py
退出码：0=四个点都抓到，1=有未抓到的。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(ROOT, "scripts", "check_comment_hygiene.py")
TARGET_A = os.path.join(ROOT, "probes", "_rev_scope_probe_a.py")
TARGET_B = os.path.join(ROOT, "scripts", "_rev_scope_probe_b.py")
TARGET_C = os.path.join(ROOT, "omegaforge", "_rev_scope_probe_c.py")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    assert open(path, encoding="utf-8").read() == text, f"写入未落盘: {path}"


def load():
    sys.argv = ["x"]
    spec = importlib.util.spec_from_file_location("ch", SCANNER)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def main():
    m = load()
    from pathlib import Path
    base_fails, _ = m.scan(Path(ROOT))
    print(f"基线命中数：{len(base_fails)}")
    check("基线：全仓整洁（注入前应为 0）", len(base_fails) == 0,
          f"命中 {len(base_fails)}")

    # A / B / C：各注入一处，扫描必须从 0 变成至少 1
    for tag, path, text in (
        ("A 探针目录里的作业记录", TARGET_A,
         '"""检查脚本示例。"""\n# 上一轮这里会报错。\nX = 1\n'),
        ("B 脚本目录里的作业记录", TARGET_B,
         '"""脚本示例。"""\n# 此前这里会返回空列表。\nY = 2\n'),
        ("C 斜杠式章节引用", TARGET_C,
         '"""模块示例。"""\n# 与第 33/38/40 章一致，此处不做改动。\nZ = 3\n'),
    ):
        write(path, text)
        fails, _ = m.scan(Path(ROOT))
        hit = [f for f in fails if os.path.basename(path) in f[1]]
        check(tag, len(hit) >= 1,
              f"命中 {[ (h[1], h[2]) for h in hit ][:3]}")
        os.remove(path)
        bp = path + ".tmp"
        if os.path.exists(bp):
            os.remove(bp)

    # D：把两个目录从扫描范围里去掉，命中数应下降
    src = open(SCANNER, encoding="utf-8").read()
    old = '    for extra in ("probes", "scripts"):'
    new = '    for extra in ():'
    assert old in src, "范围注入锚点未命中"
    write(SCANNER, src.replace(old, new))
    try:
        m2 = load()
        fails2, _ = m2.scan(Path(ROOT))
        # 去掉范围后，重新注入 A 类问题应当查不到
        write(TARGET_A, '"""检查脚本示例。"""\n# 上一轮这里会报错。\nX = 1\n')
        fails3, _ = m2.scan(Path(ROOT))
        hit3 = [f for f in fails3 if os.path.basename(TARGET_A) in f[1]]
        check("D 接线：去掉 probes/ scripts/ 后同类问题查不到（证明范围真的生效）",
              len(hit3) == 0, f"仍命中 {len(hit3)}")
    finally:
        write(SCANNER, src)
        for p in (TARGET_A, TARGET_B):
            if os.path.exists(p):
                os.remove(p)

    print(f"\n=== 汇总 ===\n  {sum(1 for _, ok in RESULTS if ok)}/{len(RESULTS)} 通过")
    for name, ok in RESULTS:
        if not ok:
            print(f"  未通过：{name}")
    return 0 if all(ok for _, ok in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
