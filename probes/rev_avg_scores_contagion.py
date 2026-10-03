#!/usr/bin/env python3
"""校验：脏评分维度只应影响自己，不得连累合法维度。

## 为什么需要

`test_non_dict_scores_does_not_crash_distill` 原先只调用 `_run()`
而不看结果。这样"把两个维度一起清零"同样能通过——它不崩溃，
却把合法的那一维也抹掉了，照样扭曲结论。断言因此是空壳。

本脚本注入"任一维度脏则两维一并清零"的写法，确认补强后的
断言会失败。若注入后测试仍全绿，说明守卫依旧是空转。

## 用法

    python3 probes/rev_avg_scores_contagion.py

退出码 0 = 抓到（注入后测试失败）；1 = 没抓到或环境异常。
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "omegaforge" / "distill" / "engine.py"
TEST = ROOT / "tests" / "test_engine_contract.py"
NODE = ("tests/test_engine_contract.py::"
        "test_non_dict_scores_does_not_crash_distill")

OLD = '''        return {"a": _avg_scores(scores.get("A", {})),
                "b": _avg_scores(scores.get("B", {})),'''

NEW = '''        _a = scores.get("A", {})
        _b = scores.get("B", {})
        if not isinstance(_a, dict) or not isinstance(_b, dict):
            _a, _b = {}, {}
        return {"a": _avg_scores(_a),
                "b": _avg_scores(_b),'''


def pytest_env() -> dict:
    """解析 pytest 运行环境（沙盒 /tmp 会被回收，不能硬编码路径）。"""
    import pathlib as _pl
    import sys as _s
    _root = str(_pl.Path(__file__).resolve().parent.parent)
    if _root not in _s.path:
        _s.path.insert(0, _root)
    from probes._pytest_env import env as _env
    return _env()


def main() -> int:
    src = ENGINE.read_text(encoding="utf-8")
    if OLD not in src:
        print("!! 锚点未命中，engine.py 结构已变，无法注入")
        return 1

    backup = tempfile.mkdtemp(prefix="rev_avg_")
    shutil.copy2(ENGINE, Path(backup) / "engine.py")
    try:
        patched = src.replace(OLD, NEW, 1)
        try:
            ast.parse(patched)
        except SyntaxError as e:
            print(f"!! 注入后语法损坏，放弃：{e}")
            return 1
        ENGINE.write_text(patched, encoding="utf-8")

        rc, failed, summary = pytest_run(NODE)
        caught, why = verdict(rc, failed, [NODE])
        print("注入「脏维度连累合法维度」后：")
        print(f"  {summary}")
        print(f"\n{'✓' if caught else '✗'} "
              f"{'抓到' if caught else '没抓到'}（{why}）")
        return 0 if caught else 1
    finally:
        shutil.copy2(Path(backup) / "engine.py", ENGINE)
        try:
            ast.parse(ENGINE.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"!! 还原失败：{e}")
        shutil.rmtree(backup, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())