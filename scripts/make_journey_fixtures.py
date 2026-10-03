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
import sys
import tempfile
from pathlib import Path

SKILL_MD = "---\nname: {name}\ndescription: {desc}\nversion: 1.0\n---\n{body}\n"


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
    lines = [
        f"export OF_JOURNEY_HOME={home}",
        f"export OF_SKILL_A={a}",
        f"export OF_SKILL_B={b}",
        f"export OF_JOURNEY_BASE={base}",
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
