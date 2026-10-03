#!/usr/bin/env python3
"""扩展交互审计的回退校验：确认它的断言真能抓到对应问题。

被验脚本覆盖的是设置、对话、技能安装、蒸馏发起、待办优先级、记忆页签
等三十余项真实交互。没有回退校验时，"断言通过"只能说明界面当前是这样，
无法说明它是否真的盯得住对应的问题——审计本身是否具备判定能力，需要单独
验。

校验点：

A 供应商下拉退回内部标识   设置页改回显示 name，审计必须判失败，且失败项
                          点名"可读名称而非内部标识"
B 路径不存在提示夹带英文栈 技能安装对不存在的路径抛出带英文调用栈的异常，
                          审计必须判失败，且失败项点名"英文调用栈"
C 准备阶段接线失效         ensure_run 恒失败，审计必须以非 0 退出并说明
                          "准备阶段未就绪"，而不是带着空页面继续断言
D 基线                    不注入，审计必须全部通过

用法：python3 probes/rev_interaction_rest.py [A|B|C|D|all]
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "probes", "frontend_render", "audit_interaction_rest.py")
SETTINGS = os.path.join(ROOT, "frontend", "src", "pages", "SettingsPage.tsx")
MANAGER = os.path.join(ROOT, "omegaforge", "skills", "manager.py")
AI = os.path.join(ROOT, "probes", "frontend_render", "audit_interaction.py")

# 备份必须放在持久目录：/tmp 里的备份会在调用之间被回收，注入与还原之间
# 一旦被打断，目标文件就永久停在已注入状态（已发生过一次：A 校验点被中断
# 后，SettingsPage.tsx 一直带着注入，此后每次都报"找不到锚点"）。
BACKUP_DIR = "/data/workspace/.rev_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)
BACKUPS = {
    SETTINGS: os.path.join(BACKUP_DIR, "rest_settings.tsx"),
    MANAGER: os.path.join(BACKUP_DIR, "rest_manager.py"),
    AI: os.path.join(BACKUP_DIR, "rest_ai.py"),
}

# A：下拉改回直接显示内部标识，中文名不用
A_OLD = "<span>{labelOf(p) || id}</span>"
A_NEW = "<span>{id}</span>"

# B：把英文调用栈塞进一条会原样透出的消息里。
#
# 不能用 FileNotFoundError 带英文消息：非 UserError 的异常一律走 _classify
# 转成固定中文短句，原始消息根本到不了界面，注入后审计必然通过。
# 真正会外泄的是"消息体由原始文本拼出来"的那类路径——UserError 原样透出，
# 正好对应这种情形。
B_OLD = '            raise FileNotFoundError("skill source not found")'
B_NEW = ('            raise UserError("Traceback (most recent call last):\\n'
         '  File \\"/srv/app/omegaforge/skills/manager.py\\", line 273\\n'
         'No such file or directory")')

# C：准备阶段恒失败
C_OLD = "def ensure_run("
C_NEW = "def ensure_run(_unused_anchor=None, **kw):\n    return False, \"注入：准备阶段恒失败\"\ndef _ensure_run_orig("


def _atomic_write(path: str, text: str):
    tmp = path + ".revtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def heal():
    """清掉遗留的注入痕迹。

    注入与还原之间一旦被打断，目标文件会停在已注入状态，此后基线恒红、
    校验失去意义。开局无条件还原，并重试以应对文件系统尚未落定的情况。

    备份文件本身可能已被回收（放在临时目录时发生过），这时改用 git 还原：
    这些文件都在版本控制内，HEAD 就是可信的干净版本。
    """
    for path, backup in BACKUPS.items():
        if not os.path.exists(backup):
            rel = os.path.relpath(path, ROOT)
            subprocess.run(["git", "checkout", "--", rel], cwd=ROOT,
                           capture_output=True, text=True)
            if os.path.exists(backup):
                continue
            continue
        for attempt in range(5):
            try:
                shutil.copyfile(backup, path + ".revtmp")
                os.replace(path + ".revtmp", path)
                os.remove(backup)
                print(f"  [自愈] 还原 {os.path.basename(path)}")
                break
            except OSError as e:
                if attempt == 4:
                    raise SystemExit(
                        f"还原 {os.path.basename(path)} 失败：{e}")
                time.sleep(3)


def inject(path: str, old: str, new: str):
    src = open(path, encoding="utf-8").read()
    if old not in src:
        raise SystemExit(f"注入失败：{os.path.basename(path)} 里找不到锚点")
    shutil.copyfile(path, BACKUPS[path])
    _atomic_write(path, src.replace(old, new, 1))
    return src


def restore():
    for path, backup in BACKUPS.items():
        if os.path.exists(backup):
            for attempt in range(5):
                try:
                    shutil.copyfile(backup, path + ".revtmp")
                    os.replace(path + ".revtmp", path)
                    os.remove(backup)
                    break
                except OSError as e:
                    if attempt == 4:
                        raise SystemExit(
                            f"还原 {os.path.basename(path)} 失败：{e}")
                    time.sleep(3)


def rebuild():
    """把仓库源码同步到前端环境并重建产物。

    两处目录必须都动：注入打在仓库源码（frontend/src）上，而审计读的产物
    由前端环境（/data/workspace/fe_env）构建。只改一处会让 A 校验点在旧
    产物上跑，注入压根没进去。

    构建不走 npx：该环境下 node_modules/.bin 为空，npx 报 command not
    found，直接以模块方式调用 vite 的入口才通。
    """
    src = os.path.join(ROOT, "frontend", "src")
    fe = os.environ.get("FE_ENV", "/data/workspace/fe_env")
    if not os.path.isdir(os.path.join(fe, "node_modules")):
        raise SystemExit(f"前端环境缺失：{fe}（A 校验点需要重新构建产物）")
    # 不用 rsync：该环境里没有这个命令。改按文件逐个覆盖，并在覆盖前删掉
    # 目标目录里的旧文件，否则仓库侧已删除的文件会留在前端环境里参与构建。
    dst = os.path.join(fe, "src")
    for dirpath, _dirnames, filenames in os.walk(dst):
        for name in filenames:
            os.remove(os.path.join(dirpath, name))
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in filenames:
            shutil.copy2(os.path.join(dirpath, name), os.path.join(target, name))
    p = subprocess.run(
        ["node", "node_modules/vite/bin/vite.js", "build"],
        cwd=fe, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        raise SystemExit("前端构建失败：" + (p.stdout + p.stderr)[-800:])


def run_audit():
    p = subprocess.run([sys.executable, AUDIT], cwd=ROOT,
                       capture_output=True, text=True, timeout=560)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def failures(out: str) -> list:
    return [l.strip() for l in out.splitlines() if "[FAIL]" in l]


def anchor_supplier_id():
    """A：下拉退回内部标识，必须被判失败且点名。"""
    heal()
    inject(SETTINGS, A_OLD, A_NEW)
    try:
        rebuild()
        rc, out = run_audit()
    finally:
        restore()
        try:
            rebuild()
        except SystemExit:
            pass
    if rc == 0:
        return "下拉显示内部标识后审计仍判通过"
    names = " ".join(failures(out))
    if "可读名称而非内部标识" not in names:
        return f"审计判失败，但不是因为供应商名称：{names[:200]}"
    return None


def anchor_english_stack():
    """B：提示夹带英文调用栈，必须被判失败且点名。"""
    heal()
    inject(MANAGER, B_OLD, B_NEW)
    try:
        rc, out = run_audit()
    finally:
        restore()
    if rc == 0:
        return "提示夹带英文栈后审计仍判通过"
    names = " ".join(failures(out))
    if "英文调用栈" not in names:
        return f"审计判失败，但不是因为英文调用栈：{names[:200]}"
    return None


def anchor_prep_wiring():
    """C：准备阶段恒失败，必须终止并说明，不能带着空页面继续。"""
    heal()
    inject(AI, C_OLD, C_NEW)
    try:
        rc, out = run_audit()
    finally:
        restore()
    if rc == 0:
        return "准备阶段失效后审计仍判通过（会带着空页面继续断言）"
    if "准备阶段未就绪" not in out:
        return f"审计判失败，但未说明准备阶段未就绪：{out[-200:]!r}"
    return None


def anchor_baseline():
    """D：不注入，审计必须全部通过。"""
    heal()
    rc, out = run_audit()
    if rc != 0:
        tail = [l.strip() for l in out.splitlines() if l.strip()][-3:]
        return "基线未通过：" + " | ".join(tail)
    m = re.search(r"(\d+)/(\d+) 通过", out)
    if not m:
        return "基线通过，但没读到通过项数（无法发现用例被删除）"
    if m.group(1) != m.group(2) or int(m.group(1)) < 30:
        return f"基线通过项数异常：{m.group(0)}"
    return None


ANCHORS = {
    "A": ("供应商退回内部标识", anchor_supplier_id),
    "B": ("提示夹带英文栈", anchor_english_stack),
    "C": ("准备阶段接线失效", anchor_prep_wiring),
    "D": ("基线", anchor_baseline),
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
        sys.stdout.flush()
    print(f"\n校验点 {len(keys)} 个，精确抓到 {len(keys) - len(bad)} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
