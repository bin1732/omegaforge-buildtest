"""终态守卫的校验。

每个校验点 = 撤掉一处修复。守卫若真的有效，撤掉之后必须出现失败项；
若撤掉之后仍然全绿，说明守卫是空转。

注入后一律先做语法校验：注入把文件写坏时，pytest 会以 collection error
退出（rc=2），看起来也是"红了"，但那不是抓到——必须区分开。

用法：python probes/rev_job_thread_terminal.py
退出码：0 = 全部校验点按预期抓到。
"""

from __future__ import annotations

import sys

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402
SERVER = ROOT / "omegaforge" / "server.py"
RUNMOD = ROOT / "omegaforge" / "core" / "run.py"
TEST = "tests/test_job_thread_terminal.py"
T = TEST + "::"
PYTEST = "python"


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


def _check_syntax(p: pathlib.Path) -> None:
    ast.parse(_read(p))


def run_tests() -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：rc==1 只说明有用例失败，说明不了失败的是不是预期那
    一条。收集阶段报错时 rc 是 2、一条没跑，同样不能算抓到。
    """
    return pytest_run(TEST)


def pytest_env():  # 兼容既有调用方
    return {}


def anchor(name: str, apply_fn, expect: list) -> bool:
    orig_server = _read(SERVER)
    orig_run = _read(RUNMOD)
    try:
        apply_fn()
        _check_syntax(SERVER)
        _check_syntax(RUNMOD)
        rc, failed, tail = run_tests()
    finally:
        _write(SERVER, orig_server)
        _write(RUNMOD, orig_run)

    caught, why = verdict(rc, failed, expect)
    flag = "抓到" if caught else "未抓到"
    print(f"  [{name}] {flag}  rc={rc}  {tail}  （{why}）")
    return caught


def move_setup_outside_try() -> None:
    """把准备阶段整段移回错误处理之外（还原成未接通时的结构）。"""
    lines = _read(SERVER).splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines)
                 if l.strip() == "try:" and "准备阶段必须在错误处理之内"
                 in lines[i + 1])
    end = next(i for i, l in enumerate(lines)
               if "genome, report = engine.distill(" in l)
    block = lines[start + 1:end]
    dedented = [(l[4:] if l.startswith("    ") else l) for l in block]
    # 去掉 try:，把准备阶段放在它前面，再让 try: 回到蒸馏调用之前
    new = lines[:start] + dedented + ["    try:\n"] + lines[end:]
    _write(SERVER, "".join(new))


def drop_ensure_terminal() -> None:
    s = _read(SERVER)
    old = 'run.ensure_terminal("任务未跑完，执行线程已结束")'
    assert old in s
    _write(SERVER, s.replace(old, "pass  # 注入：撤掉兜底终态"))


def drop_clear_active() -> None:
    s = _read(SERVER)
    old = "            clear_active(jid)"
    assert old in s
    _write(SERVER, s.replace(old, "            pass  # 注入：撤掉登记清理", 1))


def always_interrupt() -> None:
    """防修过头：终态判定失效，成功任务也会被改成中断。"""
    s = _read(RUNMOD)
    old = "        if self._status in TERMINAL:\n            return False"
    assert old in s
    _write(RUNMOD, s.replace(old, "        if False:\n            return False", 1))


def main() -> int:
    base_rc, _f, base_tail = run_tests()
    print(f"基线（未注入）：rc={base_rc}  {base_tail}")
    if base_rc != 0:
        print("基线未通过，反向验证无意义")
        return 1

    print("=== 反向验证 ===")
    results = [
        anchor("A 准备阶段移回 try 之外", move_setup_outside_try,
               [T + "test_setup_phase_failure_still_lands_terminal"]),
        anchor("B 撤掉 finally 兜底终态", drop_ensure_terminal,
               [T + "test_error_handler_failure_still_lands_terminal"]),
        anchor("C 撤掉活跃登记清理", drop_clear_active,
               [T + "test_setup_phase_failure_still_lands_terminal",
                T + "test_error_handler_failure_still_lands_terminal",
                T + "test_successful_run_is_not_overwritten"]),
        anchor("D 终态判定失效（防修过头）", always_interrupt,
               [T + "test_successful_run_is_not_overwritten",
                T + "test_ensure_terminal_does_not_rewrite_existing_terminal"]),
    ]
    print("=== 汇总 ===")
    print(f"  {sum(results)}/{len(results)} 按预期")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())