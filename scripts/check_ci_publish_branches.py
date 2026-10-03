#!/usr/bin/env python3
"""校验 CI 的「回传分支」写法：日志与产物分支必须从远端已有分支继续。

## 为什么单独查这一条

日志分支与产物分支都是"回传通道"，不是源码分支。用 `git checkout -B X`
从当前源码重建，会把整棵源码树提交进去：

- 分支条目随源码规模增长，取回时要先捞一堆无关文件；
- 分支上源码与产物并存时，无从判断该验哪一个；
- 覆盖已有内容后，失败那轮的日志无法复查。

这类失效没有任何构建步骤会报错：分支建得好好的、commit 也成功，
只是里面混进了不该有的东西。

## 判定

1. 发布分支必须从远端已有分支继续（出现 `git fetch origin <分支>`）；
2. 不得用 `git checkout -B <分支>` 重建该分支；
3. 提交前必须清索引（`git rm -r --cached`），且必须出现在 `git add -f` 之前。

第 3 条管的是"结果"，前两条管的是"写法"。只查写法不够：分支一旦被污染过，
源码树会随此后每一次提交一直留下去——此时 `git fetch` 与"不用 -B"两条都
成立，而分支里仍然混着源码条目。故清索引必须单独判定。

判定针对分支名，不针对具体步骤，新增同类回传分支时同样受约束。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# REPO_ROOT 供用例在临时仓库上验证判定本身
ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parent.parent)
WF = ROOT / ".github" / "workflows"

# 回传通道分支：只应放日志或产物，不应出现源码树
PUBLISH_BRANCHES = ("ci-logs", "artifacts")


def _unlocatable(path: Path) -> list[str]:
    """定位不到步骤时不能静默放行。

    工作流一旦改结构（换键名、改用可复用工作流），按 YAML 步骤定位就会取不
    到任何脚本，此时若返回空，这一条会整体失效而症状只有"一直绿"——与它要
    抓的失效同型。故取不到时按原文判：只要还看得到回传分支与 add，就报出来。
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    if any(br in raw for br in PUBLISH_BRANCHES) and "git add -f" in raw:
        return [f"{path.name}: 无法按步骤定位回传脚本，清索引这一条未生效"]
    return []


def prune_problems(path: Path) -> list[str]:
    """回传步骤在 `git add -f` 之前必须清索引。

    按 YAML 步骤定位，而不是在整个文件里找：一个文件里有多个回传步骤，
    全文只要出现过一次清索引就会让所有步骤都判通过。
    """
    # 无 yaml 时不得返回空：那会让这一条整条静默失效，而症状只有"一直绿"——
    # 与它要抓的失效同型。CI runner 上曾因此四次全部判通过（本地有 pyyaml
    # 所以本地看不出来）。缺依赖必须走 _unlocatable 按原文判，宁可多报。
    try:
        import yaml
    except ImportError:
        return _unlocatable(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return _unlocatable(path)
    if not isinstance(doc, dict):
        return _unlocatable(path)

    out: list[str] = []
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return _unlocatable(path)
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            script = step.get("run") or ""
            if not isinstance(script, str):
                continue
            if not any(br in script for br in PUBLISH_BRANCHES):
                continue
            add_at = _first_index(script, "git add -f")
            if add_at is None:
                continue
            prune_at = _first_index(script, "git rm -r --cached")
            if prune_at is None:
                out.append(
                    f"{path.name}: 步骤「{step.get('name') or job_name}」回传时未清索引"
                    f"（分支历史上的源码树会一直留在后面每一次提交里）"
                )
            elif prune_at > add_at:
                out.append(
                    f"{path.name}: 步骤「{step.get('name') or job_name}」清索引在 "
                    f"git add -f 之后（顺序反了等于没清）"
                )
    return out


def _first_index(text: str, needle: str) -> int | None:
    """返回 needle 首次出现的行号下标；注释行不参与。

    说明里引用旧写法不应被当成实际写法，否则守卫会因为注释而误放。
    """
    for i, ln in enumerate(text.splitlines()):
        if ln.lstrip().startswith("#"):
            continue
        if needle in ln:
            return i
    return None


def main() -> int:
    if not WF.is_dir():
        print(f"[ci-publish] 未找到工作流目录：{WF}")
        return 1
    files = sorted(p for p in WF.iterdir() if p.suffix in (".yml", ".yaml"))
    if not files:
        print(f"[ci-publish] 工作流目录为空：{WF}")
        return 1

    bad: list[str] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        # 剥离注释行：说明里引用旧写法不应被当成实际写法
        lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        body = "\n".join(lines)
        for br in PUBLISH_BRANCHES:
            if br not in body:
                continue
            if re.search(rf"git\s+checkout\s+-B\s+{re.escape(br)}\b", body):
                bad.append(f"{f.name}: 用 checkout -B 重建回传分支 {br}（会混入源码树并覆盖上一轮内容）")
            if not re.search(rf"git\s+fetch\s+origin\s+{re.escape(br)}\b", body):
                bad.append(f"{f.name}: 回传分支 {br} 未从远端已有分支继续")
        bad.extend(prune_problems(f))

    if bad:
        print("回传分支写法校验未通过：")
        for b in bad:
            print(f"  ✗ {b}")
        return 1
    print("回传分支写法校验通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
