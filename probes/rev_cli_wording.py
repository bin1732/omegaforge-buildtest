"""命令行文案守卫的校验。

每个校验点 = 撤掉一处中文化。守卫若真的有效，撤掉之后必须出现失败项。

注入后一律先做语法校验：注入把文件写坏时 pytest 以 collection error 退出
（rc=2），看起来也是"红了"，但那不是抓到——必须区分开。

用法：python probes/rev_cli_wording.py
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST = "tests/test_cli_wording_contract.py"
T = TEST + "::"
sys.path.insert(0, str(ROOT))
CLI = ROOT / "omegaforge" / "cli.py"


from probes._rev_verdict import pytest_run, verdict  # noqa: E402


def pytest_env() -> dict:
    """解析 pytest 运行环境（沙盒 /tmp 会被回收，不能硬编码路径）。"""
    import pathlib as _pl
    import sys as _s
    _root = str(_pl.Path(__file__).resolve().parent.parent)
    if _root not in _s.path:
        _s.path.insert(0, _root)
    from probes._pytest_env import env as _env
    return _env()


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: pathlib.Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def _run() -> tuple[int, str]:
    rc, _failed, tail = pytest_run(TEST)
    return rc, tail


def anchor(name: str, old: str, new: str, expect: list) -> tuple[bool, str]:
    """撤掉一处中文化，确认**指定的**用例失败。

    判定不能用 rc：注入把模块写坏时 rc 同样非零，但用例一条没跑，那不是
    抓到。必须点名到具体用例。
    """
    orig = _read(CLI)
    if old not in orig:
        return False, "注入点不存在（锚点失效，须重定位）"
    try:
        _write(CLI, orig.replace(old, new, 1))
        ast.parse(_read(CLI))
        rc, failed, tail = pytest_run(TEST)
    finally:
        _write(CLI, orig)
    caught, why = verdict(rc, failed, expect)
    print(f"  [{name}] {'抓到' if caught else '未抓到'}  rc={rc}  {tail}"
          f"  （{why}）")
    return caught, why


def main() -> int:
    rc, tail = _run()
    print(f"基线：rc={rc}  {tail}")
    if rc != 0:
        print("基线未通过，反向验证无意义")
        return 1

    print("=== 反向验证 ===")
    results = [
        # A：一个子命令说明退回英文
        anchor("A 子命令说明退回英文",
               'help="从源 Agent 提炼能力"', 'help="distill any agent source"',
               [T + "test_no_english_help_line"]),
        # B：usage 前缀退回英文
        anchor("B usage 前缀退回英文",
               '"用法：" if prefix is None else prefix',
               '"usage: " if prefix is None else prefix',
               [T + "test_usage_prefix_is_chinese"]),
        # C：报错不再翻译
        anchor("C 报错翻译失效",
               "    for pattern, cn in _ARGPARSE_MSG_CN:",
               "    for pattern, cn in ():",
               [T + "test_known_patterns_translated",
                T + "test_real_cli_error_is_chinese"]),
        # D：去掉一个参数的说明（新增参数忘写 help 的形态）
        anchor("D 去掉参数说明",
               'd.add_argument("--out", default="output", help="结果输出目录")',
               'd.add_argument("--out", default="output")',
               [T + "test_every_argument_has_help"]),
        # E：小节标题退回英文
        anchor("E 小节标题退回英文",
               'self._positionals.title = "位置参数"',
               'self._positionals.title = "positional arguments"',
               [T + "test_section_titles_are_chinese"]),
    ]
    print("=== 汇总 ===")
    caught = [ok for ok, _why in results]
    print(f"  {sum(caught)}/{len(caught)} 按预期")
    return 0 if all(caught) else 1


if __name__ == "__main__":
    sys.exit(main())