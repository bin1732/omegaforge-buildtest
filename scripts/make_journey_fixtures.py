#!/usr/bin/env python3
"""生成人类旅程所需的真实目录，并把绝对路径以 shell 可 source 的形式输出。

为什么必须由 Python（而非 shell）来定这些路径：
    旅程的后端是 Python，技能安装走 os.path.isfile(source_dir + '/SKILL.md')。
    runner 上 shell 是 Git Bash，它的 /tmp 指向 %TEMP%
    （如 C:\\Users\\runneradmin\\AppData\\Local\\Temp），而 Windows 版 Python
    看到的 '/tmp/jskill-a' 是 'C:\\tmp\\jskill-a' —— 两者不是同一个地方。
    shell 建好的目录 Python 根本看不见，于是报
    「该路径下没有可用的技能包（缺少技能说明文件）」，
    症状是"安装功能坏了"，根因却是两边文件系统视图不同（run77 真实失败）。

    由 Python 自己用 tempfile 建目录并回传绝对路径，保证生产方与消费方
    看到的是同一个路径。

为什么技能名只允许小写字母/数字/连字符/下划线：
    manager.install 会校验；用中文名会被拒，而原因一度被吞成通用提示，
    症状同样是"安装功能坏了"。故此处用合法名，正文中才用中文。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_MD = "---\nname: {name}\ndescription: {desc}\nversion: 1.0\n---\n{body}\n"


def resolve_bash() -> str:
    """定位真正可用的 bash，排除 Windows 的 WSL 启动器。

    Windows 的 CreateProcess 按名字解析 "bash" 时会命中
    C:\\Windows\\System32\\bash.exe —— 那是 WSL 启动器，未装发行版时
    它不报错、退出码也可能为 0，只往 stdout 打一段英文提示。
    拿它的输出当 shell 载入结果，"路径被吃掉"与"没被吃掉"会同文案比较，
    校验恒真：全绿而下游仍失败。
    """
    for name in ("bash", "bash.exe"):
        found = shutil.which(name)
        if not found:
            continue
        low = found.replace("\\", "/").lower()
        if "/windows/" in low or "/system32/" in low:
            continue
        return found
    return ""


def export_line(name: str, value: Path) -> str:
    """导出一行，值用单引号包裹，保证 Windows 反斜杠不被 shell 当转义符吃掉。"""
    v = str(value).replace("'", "'\\''")
    return f"export {name}='{v}'"


def verify_via_shell(path: Path, pairs: dict) -> list:
    """用真实 bash 载入文件，逐个变量比对是否逐字回传。"""
    bash = resolve_bash()
    if not bash:
        return []
    bad = []
    for k, v in pairs.items():
        r = subprocess.run(
            [bash, "-c", '. "$1"; printf %s "$' + k + '"', "_", str(path)],
            capture_output=True,
            text=True,
        )
        got = r.stdout
        if got != str(v):
            bad.append(
                f"{k} 经 shell 载入后不等于原值：原={v!r} 载入后={got!r}"
            )
    return bad


def make_home(root: Path) -> Path:
    home = root / "home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    return home


def make_skill(root: Path, name: str, body: str) -> Path:
    d = root / f"jskill-{name.split('-')[-1]}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        SKILL_MD.format(name=name, desc=f"{body}描述", body=body), encoding="utf-8"
    )
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="固定基目录；不给则用临时目录")
    ap.add_argument("--out", required=True, help="输出 shell 可 source 的文件")
    args = ap.parse_args()

    base = Path(args.base) if args.base else Path(tempfile.mkdtemp(prefix="ofjourney-"))
    base.mkdir(parents=True, exist_ok=True)

    home = make_home(base)
    a = make_skill(base, "skill-a", "甲技能正文")
    b = make_skill(base, "skill-b", "乙技能正文")

    # 回传前必须确认目录与说明文件真的可被 Python 自己看到。
    # 少了这一步，"路径写错"会一路静默到界面报错才被发现。
    for d in (home, a, b):
        if not d.is_dir():
            print(f"FAIL 目录不可用：{d}")
            return 1
    for d in (a, b):
        if not (d / "SKILL.md").is_file():
            print(f"FAIL 说明文件不可用：{d / 'SKILL.md'}")
            return 1

    # 必须写成 export：脚本用 `. 文件` 载入，而不带 export 的赋值只是当前
    # shell 的变量，不会传给子进程。旅程由 node 驱动，node 读
    # process.env 拿不到这些值时，报的是"缺少 OF_SKILL_A，请先运行夹具"——
    # 而夹具明明刚跑过，症状与"夹具没生成"完全一样（run85 step 25 真失败）。
    pairs = {
        "OF_JOURNEY_HOME": home,
        "OF_SKILL_A": a,
        "OF_SKILL_B": b,
        "OF_JOURNEY_BASE": base,
    }
    lines = [export_line(k, v) for k, v in pairs.items()]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 回读校验：值必须经 shell 载入后逐字不变。
    # bash 把反斜杠当转义符，未加引号的 `export V=D:\a\b` 载入后变成
    # `D:ab` —— 后端拿这个被吃掉的路径去查技能说明文件，报
    # 「缺少技能说明文件」，症状像"安装功能坏了"（run90 真实失败日志：
    # D:aomegaforge-buildtestomegaforge-buildtest...）。
    # Python 侧自检查不出这类错：它看到的是未被 shell 处理过的原值。
    bad = verify_via_shell(out, pairs)
    if bad:
        for m in bad:
            print(f"FAIL {m}")
        return 1
    if not bad and not resolve_bash():
        # 只找到 WSL 启动器时也算没找到：它的输出不是 shell 载入结果。
        print("SKIP 未找到可用的 bash，未做 shell 回读校验"
              f"（which={shutil.which('bash')}）")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
