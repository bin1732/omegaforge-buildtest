#!/usr/bin/env python3
"""软失败步骤必须真的软：GitHub 的 bash 步骤默认带 -e。

## 补的是什么

build.yml 把界面类四个步骤（人类旅程、逐页像素、深度旅程、全量旅程）
写成软失败：跑完把返回码记进 soft-fail.txt，末尾由汇总步骤统一判红。
意图是让编译打包、装机后功能验收、覆盖安装保住数据、带运行中的应用卸载
这四段不被界面点击挡住。

原编排是串行阻断，第一个红点之后的步骤全部 skipped——一轮构建只能暴露
一个问题，修完再跑才知道下一个，而那四段与界面点击互不依赖，被跳过等于
每轮白扔掉四份真实证据。

## 真失效：写在后面的 exit 0 根本执行不到

GitHub Actions 的 bash 步骤默认以 `bash -e -o pipefail {0}` 执行。errexit
之下，命令一失败脚本立即终止，写在后面的

    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then echo ... >> soft-fail.txt; fi
    exit 0

一行都不会执行。于是**软失败在唯一需要它的场景（命令真的失败）里退化成
硬失败**，后续步骤照样 skipped。

run107 就是实证：Pixel render check 报 failure，紧接着 Deep / Full journey
与编译、打包、装机验收、卸载全部 skipped，而那四份日志一行都没有。

## 守什么

凡是把失败写进 soft-fail.txt 的 run 块，必须出现 `set +e`。

只认 `set +e`，不认"结尾有 exit 0"：exit 0 在 errexit 下到不了，按它判定
会在真实失效形态上判通过——恒真的守卫比没有更坏。

一个都没找到也必须判失败：路径写错或编排改名时，空结果会让本守卫永久
绿着，而软失败的真实状态无人知晓。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")

MARKER = "soft-fail.txt"
REQUIRED = "set +e"


def is_soft_fail(block: str) -> bool:
    """只认"往清单里追加"的步骤。

    末尾的汇总步骤也提到 soft-fail.txt，但它是读清单并判红的硬判定，
    不是软失败步骤——按"出现过文件名"判定会把它也算进来，于是守卫要求
    它加 set +e，而它恰恰必须能在读到清单时真的返回非 0。
    """
    return any(MARKER in ln and ">>" in ln for ln in block.splitlines())


def _step_blocks(text: str):
    """切出每个步骤真正会被执行的 run 块，返回 [(步骤名, run 文本)]。

    只取 run 块，不取整段步骤：步骤之间的 YAML 注释会落进上一步的块里，
    而 build.yml 里恰好有一段注释在解释软失败机制、提到了 soft-fail.txt。
    按整段判定会把那段注释算进前一步，于是一个根本不写 soft-fail.txt 的
    步骤被判"该有 set +e"——守卫在正确编排上报红，而人因此倾向于把它
    整条删掉。

    不引第三方 YAML 解析：本守卫要在 CI 与沙盒里都跑，而依赖是否装好
    不该影响它能不能指出编排失效。
    """
    lines = text.splitlines()
    blocks = []
    cur_name = None
    run_indent = None
    cur = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- name:") or stripped.startswith("- id:"):
            if cur_name is not None:
                blocks.append((cur_name, "\n".join(cur)))
            cur_name = stripped.split(":", 1)[1].strip()
            run_indent = None
            cur = []
            continue
        if cur_name is None:
            continue
        indent = len(line) - len(line.lstrip())
        if run_indent is None:
            if stripped.startswith("run:"):
                inline = stripped[len("run:"):].strip()
                if inline and inline != "|" and not inline.startswith("|"):
                    cur.append(inline)
                    blocks.append((cur_name, "\n".join(cur)))
                    cur_name = None
                else:
                    run_indent = indent
            continue
        if not stripped or indent > run_indent:
            cur.append(line)
        else:
            blocks.append((cur_name, "\n".join(cur)))
            cur_name = None
            run_indent = None
            cur = []
    if cur_name is not None:
        blocks.append((cur_name, "\n".join(cur)))
    return blocks


def check_workflow(path: str) -> list:
    """返回失效理由列表；空列表表示通过。"""
    if not os.path.isfile(path):
        return [f"工作流文件不存在：{path}（读不到必须判失败，"
                "否则本守卫在路径写错时恒绿）"]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    blocks = _step_blocks(text)
    soft = [(n, b) for n, b in blocks if is_soft_fail(b)]
    if not soft:
        return [f"{os.path.basename(path)}：未找到任何往 {MARKER} 追加的软失败步骤"
                "（空结果不得判通过）"]
    reasons = []
    for name, block in soft:
        # 只认真实的 shell 指令，注释里提到 set +e 不算：按文本命中会让
        # 正在说明这条规则的注释把守卫骗过去。
        real = [ln for ln in block.splitlines()
                if REQUIRED in ln and not ln.strip().startswith("#")]
        if not real:
            reasons.append(
                f"步骤「{name}」记了 {MARKER} 却没有 {REQUIRED} —— "
                "GitHub bash 步骤默认带 -e，命令失败时 exit 0 执行不到，"
                "软失败会退化成硬失败并让后续步骤 skipped")
    return reasons


def main(argv=None) -> int:
    paths = []
    if os.path.isdir(WORKFLOWS):
        for name in sorted(os.listdir(WORKFLOWS)):
            if name.endswith((".yml", ".yaml")):
                paths.append(os.path.join(WORKFLOWS, name))
    if not paths:
        print("FAIL: 未找到任何工作流文件 —— 守卫不得在无输入时判通过")
        return 1
    bad = []
    for p in paths:
        bad.extend(check_workflow(p))
    if bad:
        print("FAIL: 软失败步骤写法不合规")
        for r in bad:
            print("  ❌ " + r)
        return 1
    print(f"OK 软失败步骤均真的软（检查 {len(paths)} 个工作流）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
