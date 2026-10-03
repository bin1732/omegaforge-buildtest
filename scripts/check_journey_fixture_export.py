#!/usr/bin/env python3
"""旅程夹具的导出校验：子进程必须读得到这四个变量。

## 这条校验守住什么

夹具文件被 `. 文件` 载入后，其中的赋值若不带 `export`，就只是当前 shell
的变量，不会传给子进程。而界面旅程由 node 驱动，node 读 process.env 拿
不到值时报的是「缺少 OF_SKILL_A，请先运行夹具脚本」——夹具明明刚跑过，
症状与「夹具没生成」完全一样，排查方向朝生成逻辑走。

## 做法

真跑一遍夹具生成，再让 shell 载入它并 fork 一个子进程读变量。文本上盯
`export` 关键字守不住这一点：漏写一处不会有任何语法错误，只有子进程真正
读一次才看得出。

判"非空"也不够：Windows 路径含反斜杠，未加引号时会被 shell 吃掉转义，
值仍然非空，于是校验通过而下游拿到的是另一个路径。故另判该值是否真为
已存在的目录。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "make_journey_fixtures.py"
VARS = ("OF_JOURNEY_HOME", "OF_SKILL_A", "OF_SKILL_B", "OF_JOURNEY_BASE")

# 载入夹具后由子进程读变量：子进程读不到正是本校验要抓的失效形态。
_CHILD = (
    '. "$1"\n'
    "bash -c 'printf \"%s\\n\" "
    + " ".join(f'"${name}"' for name in VARS)
    + "'\n"
)


def _python() -> str:
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _bash() -> str:
    """与 make_journey_fixtures.resolve_bash 同一套：排除 WSL 启动器。

    按名字解析 "bash" 会命中 C:\\Windows\\System32\\bash.exe，
    未装发行版时它把提示打到 stdout 而非报错，校验会照常通过。
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


def check() -> list[str]:
    problems: list[str] = []
    if not FIXTURE.is_file():
        return [f"夹具脚本不存在：{FIXTURE}"]

    python = _python()
    bash = _bash()
    if not python or not bash:
        return ["未找到 python 或 bash，无法执行本校验"]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "fixtures.sh"
        proc = subprocess.run(
            [python, str(FIXTURE), "--out", str(out)],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return [f"夹具生成失败（rc={proc.returncode}）："
                    f"{(proc.stdout or '') + (proc.stderr or '')[:300]}"]
        if not out.is_file():
            return ["夹具未落盘"]

        runner = root / "read.sh"
        runner.write_text(_CHILD, encoding="utf-8")
        child = subprocess.run(
            [bash, str(runner), str(out)],
            capture_output=True, text=True, timeout=120)
        values = [line.strip() for line in (child.stdout or "").split("\n")]

        if child.returncode != 0:
            problems.append(f"子进程读取失败（rc={child.returncode}）："
                            f"{(child.stderr or '')[:200]}")
        if len(values) < len(VARS):
            problems.append(
                f"子进程只读到 {len(values)} 个值，期望 {len(VARS)} 个")
        for name, value in zip(VARS, values):
            if not value:
                problems.append(
                    f"子进程读不到 {name}：夹具变量未导出，下游会报"
                    "「请先运行夹具脚本」")
                continue
            # 只判非空守不住 Windows：export V=D:\a\b 未加引号时反斜杠被
            # shell 当转义符吃掉，值仍然非空，而后端拿这个被吃掉的路径去查
            # 技能说明文件，报「缺少技能说明文件」——症状像"安装功能坏了"
            # （run90 真实失败日志）。故必须再判该值是否真是一个目录。
            if not Path(value).is_dir():
                problems.append(
                    f"{name} 经 shell 载入后不是已存在的目录：{value!r}。"
                    "若路径含反斜杠且未加引号，shell 会把它当转义符吃掉")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print(f"旅程夹具导出校验失败（{len(problems)} 项）：")
        for item in problems:
            print(f"  FAIL {item}")
        return 1
    print("旅程夹具导出校验通过 ✓（子进程可读到全部四个变量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
