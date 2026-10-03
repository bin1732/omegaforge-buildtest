#!/usr/bin/env python3
"""首启审计的元审计：确认前提失效会被当成失败。

被验的脚本全部断言都建立在"后端确实是空的"这一前提上。而判断空的方式是
取接口返回值里的列表：请求失败时 api() 返回带 __error__ 的字典，取列表
同样是空——"接口没应答"和"确实是空的"混为一谈，审计会带着一个从未验证过
的前提跑完。

本脚本验的是"前提失效会不会被当成失败"，三个校验点：

A 接口故障  让 /api/runs 返回故障，审计必须判失败，且失败项指向接口应答
B 判定失效  把应答判定改成恒真，接口故障时必须重新被判通过。这条是防 A
            走形式：只验 A 的话，判定被摘掉后 A 依然全绿，而它恰恰是 A
            唯一依赖的东西
C 基线      不注入，审计必须全部通过

注入点在被验脚本依赖的共用模块上（api 函数），因此每次开局先还原，收尾
再还原，避免影响其它审计。

用法：python3 probes/rev_first_run.py [A|B|C|all]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FR = os.path.join(ROOT, "probes", "frontend_render", "audit_first_run.py")
AI = os.path.join(ROOT, "probes", "frontend_render", "audit_interaction.py")
BACKUP_FR = "/tmp/rev_first_run_target.py"
BACKUP_AI = "/tmp/rev_first_run_ai.py"

# A：让 /api/runs 返回故障，模拟接口没应答
ANCHOR_A_OLD = "def api(path):\n    try:"
ANCHOR_A_NEW = ('def api(path):\n'
                '    if "/api/runs" in path:\n'
                '        return {"__error__": "模拟接口故障"}\n'
                '    try:')

# B：把应答判定改成恒真，等价于把前提判定摘掉
ANCHOR_B_OLD = '"__error__" not in r'
ANCHOR_B_NEW = "True"


def _atomic_copy(src, dst):
    """先写临时文件再整体替换，避免写一半时被打断留下残缺内容。"""
    tmp = dst + ".revtmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def heal():
    """清掉遗留的注入痕迹。

    注入与还原之间一旦被中断，目标文件会停在已注入状态，此后基线恒红、
    校验失去意义，且共用模块上的改动会波及其它审计。开局先无条件还原。
    """
    for path, backup in ((FR, BACKUP_FR), (AI, BACKUP_AI)):
        if os.path.exists(backup):
            for attempt in range(5):
                try:
                    _atomic_copy(backup, path)
                    os.remove(backup)
                    print(f"  [自愈] 还原 {os.path.basename(path)}")
                    break
                except OSError as e:
                    if attempt == 4:
                        raise SystemExit(
                            f"还原 {os.path.basename(path)} 失败：{e}")
                    time.sleep(3)


def inject(path, backup, old, new):
    shutil.copyfile(path, backup)
    src = open(path, encoding="utf-8").read()
    if old not in src:
        raise SystemExit(f"注入失败：{os.path.basename(path)} 里找不到锚点")
    tmp = path + ".revtmp"
    open(tmp, "w", encoding="utf-8").write(src.replace(old, new, 1))
    os.replace(tmp, path)


def restore():
    for path, backup in ((FR, BACKUP_FR), (AI, BACKUP_AI)):
        if os.path.exists(backup):
            for attempt in range(5):
                try:
                    _atomic_copy(backup, path)
                    os.remove(backup)
                    break
                except OSError as e:
                    if attempt == 4:
                        raise SystemExit(
                            f"还原 {os.path.basename(path)} 失败：{e}")
                    time.sleep(3)


def run_audit():
    p = subprocess.run([sys.executable, FR], cwd=ROOT,
                       capture_output=True, text=True, timeout=560)
    return p.returncode, p.stdout + p.stderr


def anchor_api_error():
    """A：接口故障必须被判失败，且失败项指向接口应答。"""
    heal()
    inject(AI, BACKUP_AI, ANCHOR_A_OLD, ANCHOR_A_NEW)
    try:
        code, out = run_audit()
    finally:
        restore()
    if code == 0:
        return "接口故障后审计仍判通过：前提从未被验证"
    if "接口应答正常" not in out:
        return f"审计判失败，但不是因为应答判定：{out[-200:]!r}"
    if "未通过：后端 运行 接口应答正常" not in out:
        return "失败项没有指向运行接口，指向不明"
    return None


def anchor_guard_removed():
    """B：判定被摘掉后，接口故障必须重新被判通过。"""
    heal()
    inject(AI, BACKUP_AI, ANCHOR_A_OLD, ANCHOR_A_NEW)
    inject(FR, BACKUP_FR, ANCHOR_B_OLD, ANCHOR_B_NEW)
    try:
        code, out = run_audit()
    finally:
        restore()
    if code != 0:
        tail = [l.strip() for l in out.splitlines() if l.strip()][-2:]
        return "判定失效后审计仍判失败，A 校验点失去意义：" + " | ".join(tail)
    return None


def anchor_baseline():
    """C：不注入，审计必须全部通过。"""
    heal()
    code, out = run_audit()
    if code != 0:
        tail = [l.strip() for l in out.splitlines() if l.strip()][-3:]
        return "基线未通过：" + " | ".join(tail)
    if "接口应答正常" not in out:
        return "基线通过，但输出里没有接口应答的判定行"
    return None


ANCHORS = {
    "A": ("接口故障", anchor_api_error),
    "B": ("判定失效", anchor_guard_removed),
    "C": ("基线", anchor_baseline),
}


def main():
    heal()
    want = sys.argv[1] if len(sys.argv) > 1 else "all"
    keys = list(ANCHORS) if want == "all" else [want.upper()]
    bad = []
    for k in keys:
        name, fn = ANCHORS[k]
        try:
            reason = fn()
        except subprocess.TimeoutExpired:
            reason = "审计超时（560 秒）"
        except SystemExit as e:
            reason = str(e)
        if reason:
            bad.append((name, reason))
            print(f"  [未抓到] {name}：{reason}")
        else:
            print(f"  [抓到] {name}")
    print(f"\n校验点 {len(keys)} 个，精确抓到 {len(keys) - len(bad)} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
