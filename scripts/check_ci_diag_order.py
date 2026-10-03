#!/usr/bin/env python3
"""诊断目录顺序守卫：任何往 ci_diag/ 落日志的步骤都必须排在建立目录之后。

背景（run79 / run80 真实失败）：
    "Install voice runtime" 排在 "Prepare diagnostics dir" 之前，
    该步 `python -m pip install ... 2>&1 | tee ci_diag/00b-voice-lib.log`
    在目录尚不存在时执行 —— tee 自建文件失败，叠加 set -o pipefail 使
    整步判失败。而真正的 pip 安装是成功的。

    症状是"装不上语音运行库"，根因是步骤顺序 —— 排查方向完全相反，
    且连续两轮构建因此全程被跳过（后续 30 余步 skipped）。

判定口径（三条，各自独立，缺一即失效）：
    1. 必须存在"建立目录"的步骤（mkdir ci_diag）。找不到就失败 ——
       找不到时若判通过，整条守卫退化成恒真。
    2. 每个引用 ci_diag/ 的 run 步骤，其序号必须大于建立目录那一步。
    3. 工作流读不到或解析不了，一律失败并给出原因，不得静默通过。

为什么按 run 正文判断而不是按步骤名：
    步骤名可被改写而行为不变。按正文里真实出现的 ci_diag/ 定位，
    改名不会让守卫失效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build.yml"
DIR_NAME = "ci_diag"


def _steps(doc: dict) -> list[dict]:
    jobs = doc.get("jobs") or {}
    out: list[dict] = []
    for job in jobs.values():
        if isinstance(job, dict):
            for st in job.get("steps") or []:
                if isinstance(st, dict):
                    out.append(st)
    return out


def _run_text(step: dict) -> str:
    """取 run 正文，去掉整行 shell 注释。

    不去注释会把「注释里说明这条规则」判成违规：守卫因此在正确实现上
    变红，而人倾向于把变红的守卫整条删掉。判定只认真实执行的代码行。
    """
    run = step.get("run")
    if not isinstance(run, str):
        return ""
    return "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))


def check(path: Path = WORKFLOW) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    problems: list[str] = []

    if not path.is_file():
        return [f"读不到工作流文件：{path}"]
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # 解析失败必须报出来，不能当作"没有问题"
        return [f"工作流解析失败：{type(exc).__name__}: {exc}"]
    if not isinstance(doc, dict):
        return ["工作流顶层不是映射，无法定位步骤"]

    steps = _steps(doc)
    if not steps:
        return ["工作流里没有任何步骤"]

    creator: tuple[int, str] | None = None
    for idx, st in enumerate(steps):
        if f"mkdir" in _run_text(st) and f"{DIR_NAME}" in _run_text(st):
            creator = (idx, str(st.get("name") or f"#{idx}"))
            break

    if creator is None:
        return [f"找不到建立 {DIR_NAME} 目录的步骤（须有 mkdir ... {DIR_NAME}）"]

    for idx, st in enumerate(steps):
        text = _run_text(st)
        if f"{DIR_NAME}/" not in text:
            continue
        if idx < creator[0]:
            problems.append(
                f"步骤 #{idx + 1}「{st.get('name') or ''}」引用 {DIR_NAME}/，"
                f"但建立目录的步骤是 #{creator[0] + 1}「{creator[1]}」"
                f"—— 落日志会发生在目录存在之前"
            )
            continue
        if idx == creator[0]:
            # 同一步内建立并写入是允许的，但 mkdir 必须排在使用之前。
            # 只按"步骤序号"判会让这一步恒真：mkdir 写在 echo 之后时，
            # 目录同样尚不存在，而序号上看起来完全一样。
            lines = text.splitlines()
            mk = next((i for i, ln in enumerate(lines) if "mkdir" in ln and DIR_NAME in ln), None)
            use = next((i for i, ln in enumerate(lines) if f"{DIR_NAME}/" in ln), None)
            if mk is None or use is None or mk > use:
                problems.append(
                    f"步骤 #{idx + 1}「{st.get('name') or ''}」内部："
                    f"mkdir 必须排在首次使用 {DIR_NAME}/ 之前"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = Path(args[0]) if args else WORKFLOW
    problems = check(target)
    if problems:
        print(f"FAIL 诊断目录顺序（{len(problems)} 项）")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"OK 诊断目录顺序：所有引用 {DIR_NAME}/ 的步骤都在建立目录之后")
    return 0


if __name__ == "__main__":
    sys.exit(main())
