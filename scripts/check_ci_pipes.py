#!/usr/bin/env python3
"""CI 步骤里凡用管道落盘的，必须有退出码保护。

## 被管的失效

`cmd | tee log` 的退出码取自管道最后一段，即 tee。tee 几乎总是成功，
于是被观测的命令失败了，这一步仍然绿。日志照常落盘，从日志里能看出
失败，但从 Actions 的步骤状态上看不出来。

后果是"CI 绿"与"这些守卫是否通过"脱钩：流水线继续往下走，把有问题的
源码一路打包成安装包。

## 保护形式

两种都认：`set -o pipefail`（管道任一段失败即整段失败）与
`exit ${PIPESTATUS[0]}`（显式取首段）。只认命令与管道出现在同一段 run
内，避免把注释里的样例当成真实调用。

只采集信息、本就不该阻断的步骤可以显式放弃退出码（`|| true` 或
`exit 0`），同样算有表态。要管的是"既没传递失败、也没声明不阻断"——
那通常是照抄了别处的写法，而不是有意为之。

## 用法

    python3 scripts/check_ci_pipes.py [工作流文件]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / ".github" / "workflows" / "build.yml"

# 被观测的命令必须真的在管道里：只匹配管道符前后确有内容的写法，
# 注释与说明性文字不在扫描范围内（逐行剥离 # 之后再看）。
PIPE = re.compile(r"\|\s*(?:tee\b|&\s*tee\b)")
GUARD = ("pipefail", "PIPESTATUS")
# 显式声明本步骤不阻断：采集类步骤本就不该因目录不存在而让构建失败，
# 但要写出来，否则与"忘了处理退出码"无从区分。
WAIVE = ("|| true", "exit 0")


def strip_comment(line: str) -> str:
    """去掉行尾注释。

    只处理整行以 # 开头、或 # 前是空白且不在引号内的情形。工作流里的
    命令很少带引号内含 #，按空白切分即可覆盖实际写法。
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        return ""
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def steps(text: str) -> list[tuple[str, str, bool]]:
    """切成（步骤名, 正文, 是否 bash），按 YAML 的 "- name:" 分界。"""
    out: list[tuple[str, str, bool]] = []
    name, body, is_bash = "(未命名)", [], False
    for line in text.splitlines():
        indented = line.startswith("      ")
        if re.match(r"^\s*- name:", line):
            if body:
                out.append((name, "\n".join(body), is_bash))
            name = line.split(":", 1)[1].strip()
            body, is_bash = [], False
            continue
        if re.match(r"^\s*shell:\s*bash\s*$", line):
            is_bash = True
        if indented or line.strip().startswith(("run:", "|", "-")):
            body.append(line)
    if body:
        out.append((name, "\n".join(body), is_bash))
    return out


def unguarded(text: str) -> list[tuple[str, str]]:
    bad = []
    for name, body, _is_bash in steps(text):
        code = "\n".join(strip_comment(l) for l in body.splitlines())
        if not PIPE.search(code):
            continue
        # 保护可以写在管道之前或之后的同一段 run 里
        if any(g in code for g in GUARD + WAIVE):
            continue
        line = next(l.strip() for l in body.splitlines()
                    if PIPE.search(strip_comment(l)))
        bad.append((name, line))
    return bad


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        print(f"工作流不存在：{path}")
        return 2
    text = path.read_text(encoding="utf-8")
    # 一个步骤都认不出来时必须判失败。空文件、缩进层级不对、步骤结构变了
    # 都会走到这里，而这几种情形与"步骤都合规"的结论完全一样：无违规。
    # 读不出内容时，无违规与未检查无法区分，故在此处显式判失败。
    if not steps(text):
        print(f"{path.name}：未能从工作流里认出任何步骤，守卫无从生效")
        return 2
    bad = unguarded(text)
    if bad:
        print(f"{path.name}：{len(bad)} 处管道落盘没有退出码保护")
        for name, line in bad:
            print(f"  {name}\n      {line[:90]}")
        print("补 set -o pipefail，或取 ${PIPESTATUS[0]} 作为退出码。")
        return 1
    print(f"{path.name}：管道落盘均有退出码保护 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
