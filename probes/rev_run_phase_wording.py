#!/usr/bin/env python3
"""阶段名中文修复的校验。

判据是**有真实的 failed 数**，不是"注入成功"：测试若因语法损坏没跑起来，
退出码同样非 0，会把"没跑"误读成"抓到"。因此每次注入后强制 ast.parse 校验。

校验点
  A 事件消息退回英文阶段标识（server.on_phase） → 应抓到
  B 起始事件消息退回裸 "queued"                 → 应抓到
  C PHASE_TEXT 少收录一个真实阶段               → 应抓到（接通守卫）
  D phase_text 对未收录标识做猜测翻译           → 应抓到（防修过头）
"""

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "omegaforge" / "core" / "run.py"
SERVER = ROOT / "omegaforge" / "server.py"
GUARD = ROOT / "tests" / "test_run_phase_wording.py"

ANCHORS = [
    ("A 事件消息退回英文阶段标识", {
        "服务端阶段回调确实转了中文",
    }, [
        (SERVER,
         'run.phase_to(phase, message=f"进入阶段：{phase_text(phase)}")',
         'run.phase_to(phase, message=f"进入阶段 {phase}")'),
    ]),
    ("B 起始事件消息退回裸 queued", {
        "落盘后消息里不残留英文阶段标识",
    }, [
        (RUN,
         'r.emit("phase", phase_text("queued"), phase="queued")',
         'r.emit("phase", "queued", phase="queued")'),
    ]),
    ("C 中文表少收录一个真实阶段", {
        # 少一条会同时让该阶段的落盘断言变红
        "引擎实际进入的阶段_全部有中文说法",
        "进度表里的阶段_中文表也必须有",
        "落盘后消息里不残留英文阶段标识",
    }, [
        (RUN,
         '    "extract": "提取中",\n',
         ''),
    ]),
    ("D 未收录标识做猜测翻译", {
        "未收录的标识原样返回_不做猜测翻译",
    }, [
        (RUN,
         '    return PHASE_TEXT.get(phase, phase)',
         '    return PHASE_TEXT.get(phase, "未知阶段")'),
    ]),
]


def apply_pairs(pairs, forward):
    for path, old, new in pairs:
        src = path.read_text(encoding="utf-8")
        a, b = (old, new) if forward else (new, old)
        if a not in src:
            raise SystemExit(f"注入失败：{path.name} 里找不到锚点文本")
        shutil.copy2(path, str(path) + ".bak")
        path.write_text(src.replace(a, b, 1), encoding="utf-8")
        # 语法校验前置：语法坏了的话 failed 数是假的
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            for p, _, _ in pairs:
                shutil.move(str(p) + ".bak", p)
            raise SystemExit(f"注入后语法损坏（{e}），已回滚")


def restore(pairs):
    for path, _, _ in pairs:
        bak = str(path) + ".bak"
        if os.path.exists(bak):
            shutil.move(bak, path)


def run_guard():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", str(GUARD), "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    names = set()
    for ln in out.splitlines():
        if not ln.startswith("FAILED "):
            continue
        nm = ln.split(" ", 1)[1].split(" ")[0]
        # 形如 tests/xxx.py::test_y[param]，先去路径再去参数
        if "::" in nm:
            nm = nm.split("::", 1)[1]
        nm = re.sub(r"\[.*\]$", "", nm)
        if nm.startswith("test_"):
            nm = nm[len("test_"):]
        names.add(nm)
    m = re.search(r"(\d+) failed", out)
    return (int(m.group(1)) if m else 0), names, out


def main():
    # 自愈：清掉上一版被中断留下的注入痕迹
    for _, _, pairs in ANCHORS:
        for path, old, new in pairs:
            src = path.read_text(encoding="utf-8")
            if new in src and old not in src:
                path.write_text(src.replace(new, old, 1), encoding="utf-8")
                print(f"  [自愈] 还原上轮遗留注入：{path.name}")
            bak = str(path) + ".bak"
            if os.path.exists(bak):
                os.remove(bak)

    n0, f0, _ = run_guard()
    print(f"基线：failed={n0} 失败={sorted(f0)}")
    if n0 != 0:
        raise SystemExit("基线就不是全绿，反向验证没有意义")

    allok = True
    for name, expect, pairs in ANCHORS:
        apply_pairs(pairs, forward=True)
        try:
            n, got, out = run_guard()
        finally:
            restore(pairs)
        want = set(expect)
        ok = n > 0 and want.issubset(got) and not (got - want)
        print(f"{'  [抓到]' if ok else '  [未抓到]'} {name}: failed={n} {sorted(got)}")
        if not ok:
            allok = False
            print(f"      期望={sorted(want)}")
            print("      " + "\n      ".join(out.splitlines()[-5:]))

    n1, f1, _ = run_guard()
    print(f"还原后：failed={n1}")
    if n1 != 0:
        print("  [警告] 还原后仍未全绿，产物可能残留注入")
        allok = False
    print("RESULT:", "ALL_CAUGHT" if allok else "INCOMPLETE")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
