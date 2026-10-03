#!/usr/bin/env python3
"""校验与检查脚本共用的 pytest 环境解析。

## 为什么单独抽出来

多个校验脚本各自硬编码 `PYTHONPATH=/tmp/pylibs`。沙盒的 /tmp 在
命令之间会被回收，于是下一次命令里 pytest 就没了。此时 pytest 以
"No module named pytest" 退出（rc=1），脚本拿到 rc=1 后：

- 有基线检查的，报"基线未通过，校验无意义"；
- 没有基线检查的，把 rc=1 直接当成"注入后变红了 → 抓到"。

第二种最危险：**环境缺包被当成守卫有效**。检验 rev_orphan_reclaim 就
是这个形态——四个校验点里三个报"未抓到"、一个报"校验点失效"，全部是环境
缺包造成的假结果，与产品无关。

## 做法

解析顺序：

1. 当前环境已能 import pytest → 直接沿用，不额外设置；
2. 工作区 `.pylibs`（已加入 .gitignore）已装好 → 指过去；
3. 都没有 → 调 `_ensure_pytest.sh` 装到工作区，再指过去。

返回应注入子进程的环境字典（只补 PYTHONPATH，不动其余变量）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSIST = ROOT / ".pylibs"


def _importable() -> bool:
    import importlib.util
    return importlib.util.find_spec("pytest") is not None


def ensure() -> Path | None:
    """保证 pytest 可用，返回需要加入 PYTHONPATH 的目录（可 None）。"""
    if _importable():
        return None
    probe = subprocess.run(
        [sys.executable, "-c", "import pytest"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(PERSIST)},
    )
    if probe.returncode == 0:
        return PERSIST
    # 落到工作区而非 /tmp：后者在命令之间会被回收。
    sh = ROOT / "probes" / "_ensure_pytest.sh"
    r = subprocess.run(["sh", str(sh), str(PERSIST)],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"pytest 环境准备失败：{r.stderr.strip()[-200:]}")
    return PERSIST


def env() -> dict[str, str]:
    """返回给 subprocess 用的环境字典。"""
    e = dict(os.environ)
    d = ensure()
    if d is not None:
        e["PYTHONPATH"] = str(d) + (
            ":" + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    return e


if __name__ == "__main__":
    print(env().get("PYTHONPATH", "(沿用当前环境)"))
