"""校验脚本共用的自愈：只在有备份时还原，其余情况交给人判断。

## 为什么不能用 HEAD 覆盖一切

"注入残留"与"编写中的源码改动"在 `git diff` 下完全同形 —— 两者都是工作
区与 HEAD 不一致。一律 `git checkout` 覆盖会把当次正在写的改动一并抹掉，
而抹掉之后的症状是用例变红，排查方向指向产品代码，而不是跑过的脚本。

## 做法

1. 有 `.bak`（脚本自己留下的备份）→ 移动回去，这是确定的残留；
2. 没有 `.bak` 而文件仍与 HEAD 有差异 → 列出文件名并中止。

返回 False 表示应中止整轮校验。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def dirty_names(files: list[Path], root: Path) -> list[str]:
    """取与 HEAD 有差异的文件名（相对仓库根）。"""
    rel = [str(p.relative_to(root)) for p in files]
    r = subprocess.run(["git", "diff", "--name-only", "--"] + rel,
                       cwd=str(root), capture_output=True, text=True)
    return (r.stdout or "").split()


def report_and_stop(names: list[str]) -> bool:
    """打印中止理由；返回 False 供调用方直接返回。"""
    print("  [中止] 这些文件与 HEAD 有差异，无法区分是注入残留还是"
          "编写中的改动：")
    for name in names:
        print(f"      {name}")
    print("      先提交或先手工确认，再跑本脚本")
    return False
