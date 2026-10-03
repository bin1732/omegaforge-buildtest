#!/usr/bin/env python3
"""装机体积上报步骤的行为校验：取流程里的脚本真跑，不靠文本推断。

## 这条校验守住什么

该步骤以 `exit 0` 结尾，意图是"只采集体积、不阻断流程"。而 GitHub 的
bash 入口带 `-eo pipefail`：脚本里任一管道非 0，都会让它在到达 `exit 0`
之前中止，于是这行成为死代码，步骤被判失败——报出来的失败原因却是采集
脚本自己，真正的失败被盖在后面。

另一处是口径。安装目录不存在时若照常打印 `installed_files=0`，读日志的
人会当成"装完了但一个文件都没有"，排查方向朝打包配置走；真实含义是
"压根没采集到"。两者必须分开报。

## 做法

直接取流程里该步骤的 run 脚本，用 `bash -eo pipefail` 真跑两遍：

- 目录不存在：必须退出 0、报"未采集"、不得出现 `installed_files=0`
- 目录存在：把目录字面量换成临时目录后跑，必须报出真实文件数与体积

文本推断做不到这件事——盯着脚本看不出 `-eo pipefail` 会不会提前中止，
只有真跑才看得出退出码。
"""
from __future__ import annotations

import os
import re
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

STEP_NAME = "Report installed footprint"
# 步骤里写死的装机目录字面量（Git Bash 下等价于 C:\of_install）。
INSTALL_DIR = "/c/of_install"
# 场景目录经该变量传入：替换成脚本里的目录字面量，而不是把真实路径
# 写进脚本文本（Windows 路径的反斜杠会被 bash 当转义符吃掉）。
DIR_ENV = "OF_FOOTPRINT_DIR"
WORKFLOW_DEFAULT = ".github/workflows/build.yml"


def find_step_run(path: Path, step_name: str = STEP_NAME) -> str | None:
    """按步骤名取出 run 脚本体。

    用文本而非 YAML 解析：流程文件里的 `on:` 在 YAML 1.1 下会被读成布尔
    键，解析结果随库版本变化；而这里只需要一段缩进块，文本更稳。
    """
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return None

    idx = None
    for i, line in enumerate(lines):
        if line.strip() == f"- name: {step_name}":
            idx = i
            break
    if idx is None:
        return None

    run_i = None
    for j in range(idx + 1, min(idx + 12, len(lines))):
        if lines[j].strip().startswith("run: |"):
            run_i = j
            break
        # 遇到下一个步骤就停：说明该步骤没有 run 块
        if lines[j].strip().startswith("- name:"):
            return None
    if run_i is None:
        return None

    run_indent = len(lines[run_i]) - len(lines[run_i].lstrip())
    body: list[str] = []
    for line in lines[run_i + 1:]:
        if not line.strip():
            body.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= run_indent:
            break
        body.append(line[indent:] if indent >= run_indent + 2 else line.lstrip())
    return "\n".join(body).strip()


def _bash() -> str:
    """排除 Windows 的 WSL 启动器（与 check_journey_fixture_export 同一套）。

    按名字解析 "bash" 会命中 C:\\Windows\\System32\\bash.exe（WSL 启动器），
    未装发行版时它把提示打到 stdout 而不报错。本校验靠"真跑一段脚本"判定，
    拿到那段提示当输出时，失败与通过会长得一模一样。
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


def run_script(script: str, cwd: Path, env: dict | None = None
               ) -> tuple[int, str]:
    """用 bash -eo pipefail 真跑一段脚本，返回退出码与合并输出。"""
    bash = _bash()
    if not bash:
        return -1, "未找到 bash，无法执行"
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8")
    try:
        handle.write(script + "\n")
        handle.close()
        proc = subprocess.run(
            [bash, "-eo", "pipefail", handle.name],
            cwd=str(cwd), capture_output=True, text=True, timeout=120,
            env=env if env is not None else os.environ.copy(),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _with_dir(path: Path) -> dict:
    """把目录经环境传进脚本，不写进脚本文本。

    写进文本会被 bash 按词法转义：Windows 路径里的反斜杠会被当成转义符
    吃掉，路径随之变形，find 找不到任何文件，报出 installed_files 为空
    ——看着像采集脚本坏了，实为本校验自己把路径写坏了。经环境传入的值
    不经过词法解析，两个平台行为一致。
    """
    """把目录经环境传进脚本，不写进脚本文本。

    后跟普通字母时会被吃掉，路径变成 `C:Users...`，find 找不到任何文件，
    报出 installed_files 为空——看着像"采集脚本坏了"，实为本校验自己把
    路径写坏了。经环境传入的值不经过词法解析，两个平台行为一致。
    """
    env = os.environ.copy()
    env[DIR_ENV] = str(path)
    return env


def check(workflow: Path = Path(WORKFLOW_DEFAULT)) -> list[str]:
    """返回问题列表；空列表代表通过。"""
    problems: list[str] = []

    script = find_step_run(workflow)
    if not script:
        # 定位不到必须判失败：返回空列表会让本校验在流程改名时恒为通过。
        return [f"未在 {workflow} 中定位到步骤「{STEP_NAME}」的 run 脚本"]

    if INSTALL_DIR not in script:
        problems.append(f"脚本中不含装机目录字面量 {INSTALL_DIR}")

    # 场景一：目录不存在。这是流程里该步骤的真实处境——它带 if: always()，
    # 前置步骤失败时安装压根没执行。目录经环境传入，故该场景恒定可构造，
    # 不受本机是否真装过影响。
    variant = script.replace(INSTALL_DIR, f'"${DIR_ENV}"')
    if variant == script:
        problems.append("替换目录字面量未生效，两个场景都验不到")
        return problems
    if "\\" in variant:
        problems.append("替换后的脚本含反斜杠：Windows 路径写进 bash 脚本"
                        "会被当转义符吃掉，采集到的是错路径")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = root / "no_such_install"
        rc, out = run_script(variant, root, env=_with_dir(missing))
        if rc != 0:
            problems.append(
                f"目录缺失时退出码为 {rc}：exit 0 未生效"
                "（管道非 0 时 -eo pipefail 会提前中止）")
        if "未采集" not in out:
            problems.append("目录缺失时未报「未采集」，读日志的人无从区分"
                            "「没采集到」与「装完是空的」")
        if re.search(r"installed_files=0\b", out):
            problems.append(
                "目录缺失时打印了 installed_files=0：会被读成"
                "「装完一个文件都没有」，方向反了")
        if "未找到 bash" in out:
            problems.append(out.strip())

    # 场景二：目录存在。验这一支真能报出数字。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        populated = root / "inst"
        populated.mkdir()
        (populated / "sub").mkdir()
        for k in range(3):
            (populated / f"f{k}.bin").write_bytes(b"x" * 2048)
        rc, out = run_script(variant, root, env=_with_dir(populated))
        if rc != 0:
            problems.append(f"目录存在时退出码为 {rc}，采集体积不得阻断")
        if "installed_files=3" not in out:
            problems.append(
                "目录存在时未报出真实文件数（期望 installed_files=3）")
        if not re.search(r"installed_MB=\d", out):
            problems.append("目录存在时未报出体积数值")
    return problems


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else WORKFLOW_DEFAULT)
    if not target.is_file():
        print(f"流程文件不存在：{target}")
        return 1
    problems = check(target)
    if problems:
        print(f"装机体积上报步骤校验失败（{len(problems)} 项）：")
        for item in problems:
            print(f"  FAIL {item}")
        return 1
    print(f"装机体积上报步骤校验通过 ✓（{target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
