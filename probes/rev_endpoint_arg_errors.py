"""入参文案守卫的校验。

每个校验点 = 撤掉一处修复。守卫若真的有效，撤掉之后必须出现失败项。

注入后一律先做语法校验：注入把文件写坏时 pytest 以 collection error 退出
（rc=2），看起来也是"红了"，但那不是抓到——必须区分开。

用法：python probes/rev_endpoint_arg_errors.py
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST = "tests/test_endpoint_arg_error_contract.py"
T = TEST
sys.path.insert(0, str(ROOT))
TARGETS = {
    "policy": ROOT / "omegaforge" / "tools" / "policy.py",
    "store": ROOT / "omegaforge" / "chat" / "store.py",
    "server": ROOT / "omegaforge" / "server.py",
}


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


def anchor(name: str, key: str, old: str, new: str,
           expect: list) -> tuple[bool, str]:
    """撤掉一处修复，确认**指定的**用例失败。

    判定不能用 rc：给被测模块注入一个无关的模块级未定义名，69 条用例全部
    报错，rc 同样非零——那样任何注入都会被记成抓到。必须点名。
    """
    p = TARGETS[key]
    orig = _read(p)
    if old not in orig:
        return False, "注入点不存在（锚点失效，须重定位）"
    try:
        _write(p, orig.replace(old, new, 1))
        ast.parse(_read(p))
        rc, failed, tail = pytest_run(TEST)
    finally:
        _write(p, orig)
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
        # A：权限级别校验退回 ValueError（会被压成兜底文案）
        anchor("A 权限级别退回 ValueError", "policy",
               'raise UserError(\n                "权限级别只能是："',
               'raise ValueError(\n                "权限级别只能是："',
               [T + "::test_policy_mode_lists_all_valid_levels",
                T + "::test_no_generic_fallback",
                T + "::test_message_points_at_the_field"]),
        # B：对话编号校验退回 ValueError
        anchor("B 对话编号退回 ValueError", "store",
               'raise UserError("对话编号格式不正确")',
               'raise ValueError("对话编号格式不正确")',
               [T + "::test_no_generic_fallback",
                T + "::test_message_points_at_the_field"]),
        # C：删除接口撤掉"请填写：对话"（缺编号时落到格式校验）
        anchor("C 删除接口撤掉缺项提示", "server",
               '                    raise UserError("请填写：对话")\n'
               '                self._json({"deleted": CONVS.delete(cid)})',
               '                    pass\n'
               '                self._json({"deleted": CONVS.delete(cid)})',
               [T + "::test_missing_argument_says_what_to_fill_not_malformed"]),
        # D：防修过头 —— 把正常合法请求也拒掉
        anchor("D 权限级别校验放宽到全放行", "policy",
               'if mode not in MODES:', 'if False:',
               [T + "::test_policy_mode_lists_all_valid_levels",
                T + "::test_no_generic_fallback",
                T + "::test_message_points_at_the_field"]),
    ]
    print("=== 汇总 ===")
    caught = [ok for ok, _why in results]
    print(f"  {sum(caught)}/{len(caught)} 按预期")
    return 0 if all(caught) else 1


if __name__ == "__main__":
    sys.exit(main())