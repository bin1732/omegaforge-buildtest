#!/usr/bin/env python3
"""交互审计的元审计：确认准备阶段失败会被当成失败。

交互审计存在的意义是验**有内容时**能不能用：真鼠标点击、真键盘输入、
回查后端确认落库。这些都建立在"目录里确实有运行产物"这个前提上。

而这个前提是脆弱的——它靠真跑一次蒸馏来造数据。蒸馏接口改了、任务以
失败结束、完成后没落库，都会让后续断言落在空页面上。空页面同样能渲染、
能点击，若干断言照样通过：审计会静默退化成首启审计的重复，真正该覆盖的
那一半反而没人验。

本脚本验的是"准备失败会不会被当成失败"，三个校验点：

A 接口失效  蒸馏路径改错，请求未被受理。审计必须判未通过，且说明指向
            准备阶段本身，而不是让人以为是界面列表渲染坏了
B 任务失败  蒸馏任务以 failed 结束。审计必须判未通过
C 基线      不注入，审计必须全部通过

A 与 B 是两条互相独立的路径：只验 A 的话，任务失败这一支被改回去也不会
有任何校验点变红。

用法：python3 probes/rev_frontend_interaction.py [A|B|C|all]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "probes", "frontend_render", "audit_interaction.py")
BACKUP = "/tmp/rev_frontend_interaction_target.py"

# A：把蒸馏接口改成不存在的路径，请求会落到未受理分支
ANCHOR_A_OLD = 'r = api_post("/api/distill", {'
ANCHOR_A_NEW = 'r = api_post("/api/distill_broken", {'

# B：让任务状态恒为失败，模拟蒸馏真的没跑成
ANCHOR_B_OLD = '        st = str(j.get("status") or "")'
ANCHOR_B_NEW = '        st = str(j.get("status") or "")\n        st = "failed"'


def heal():
    """清掉遗留的注入痕迹。

    注入与还原之间一旦被中断，目标文件会停在已注入状态，此后基线恒红、
    校验失去意义。因此每次开局都先无条件还原一次，让本脚本可重入。
    """
    if os.path.exists(BACKUP):
        shutil.copyfile(BACKUP, TARGET)
        os.remove(BACKUP)
        print("  [自愈] 还原上一轮遗留的注入")


def inject(old, new):
    shutil.copyfile(TARGET, BACKUP)
    src = open(TARGET, encoding="utf-8").read()
    if old not in src:
        raise SystemExit(f"注入失败：找不到锚点 {old!r}（审计脚本已变）")
    open(TARGET, "w", encoding="utf-8").write(src.replace(old, new, 1))


def restore():
    if os.path.exists(BACKUP):
        shutil.copyfile(BACKUP, TARGET)
        os.remove(BACKUP)


def run_audit():
    p = subprocess.run([sys.executable, TARGET], cwd=ROOT,
                       capture_output=True, text=True, timeout=560)
    return p.returncode, p.stdout + p.stderr


def anchor_broken_api():
    """A：蒸馏接口失效，必须判未通过且说明指向准备阶段。"""
    heal()
    inject(ANCHOR_A_OLD, ANCHOR_A_NEW)
    try:
        code, out = run_audit()
    finally:
        restore()
    if code == 0:
        return "接口失效后审计仍判通过：准备阶段被静默跳过"
    if "准备阶段" not in out:
        return "审计判未通过，但输出里没有指向准备阶段的说明"
    if "运行记录列表存在" in out and "准备阶段" not in out.split("汇总")[0]:
        return "失败原因指向界面列表，而非准备阶段"
    return None


def anchor_failed_job():
    """B：蒸馏任务以失败结束，必须判未通过。"""
    heal()
    inject(ANCHOR_B_OLD, ANCHOR_B_NEW)
    try:
        code, out = run_audit()
    finally:
        restore()
    if code == 0:
        return "任务以 failed 结束后审计仍判通过"
    if "准备阶段" not in out:
        return "审计判未通过，但输出里没有指向准备阶段的说明"
    return None


def anchor_baseline():
    """C：不注入，审计必须全部通过。"""
    heal()
    code, out = run_audit()
    if code != 0:
        tail = [l.strip() for l in out.splitlines() if l.strip()][-3:]
        return "基线未通过：" + " | ".join(tail)
    if "准备阶段" not in out:
        return "基线通过，但输出里没有准备阶段的判定行"
    return None


ANCHORS = {
    "A": ("接口失效", anchor_broken_api),
    "B": ("任务失败", anchor_failed_job),
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
