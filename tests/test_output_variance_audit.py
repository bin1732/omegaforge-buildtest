# -*- coding: utf-8 -*-
"""产物变化审计自身的守卫。

## 守的是什么

`audit_output_variance.py` 的作用是查出"与输入无关却显示为具体值"的
产物字段。它自己也可能恒真：若恒定判定写错（例如把每个源都当成
同一组取值），它会一路报通过，而什么都没查。

## 判定

三条判定各自配反例：

  * 必变字段恒定 → 必须报失败；
  * 恒定但未登记 → 必须报出（不表态不默认放行）；
  * 取值随源变化 → 不得判为恒定。
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "audit_output_variance.py")
_spec = importlib.util.spec_from_file_location("audit_output_variance", _PATH)
assert _spec and _spec.loader
audit_output_variance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_output_variance)


def _table(entries: dict) -> dict:
    """entries: {字段: 各源取值列表}；列表长度即源材料份数。"""
    return {k: {str(i): v for i, v in enumerate(vs)}
            for k, vs in entries.items()}


def test_must_vary_field_being_constant_is_reported():
    """必变字段恒定必须判失败。"""
    failed, _, _ = audit_output_variance.audit(
        _table({"genome.name": ["a", "a", "a", "a"]}))
    assert any(f == "genome.name" for f, _ in failed), (
        f"必变字段恒定却未报出：{failed}")


def test_varying_field_is_not_treated_as_constant():
    """取值随源变化不得判为恒定。"""
    failed, undeclared, consts = audit_output_variance.audit(
        _table({"genome.name": ["a", "b", "c", "d"]}))
    assert not consts, f"随输入变化的字段被判为恒定：{consts}"
    assert not failed and not undeclared


def test_undeclared_constant_is_reported():
    """恒定但未登记必须报出，不表态不能默认放行。

    只查"必变字段"的话，未登记字段会成为逃避口：把字段从必变表移到
    未登记处即可绕过，脚本随之退化成每次都通过。
    """
    _, undeclared, _ = audit_output_variance.audit(
        _table({"genome.some_future_field": ["x", "x", "x", "x"]}))
    assert any(f == "genome.some_future_field" for f, _ in undeclared), (
        f"未登记的恒定字段未被报出：{undeclared}")


def test_declared_constant_passes():
    """已登记理由的恒定字段放行。"""
    failed, undeclared, _ = audit_output_variance.audit(
        _table({"report.generation": ["1", "1", "1", "1"]}))
    assert not failed and not undeclared


def test_must_vary_table_is_non_empty():
    """必变表为空会让本脚本恒真：没有任何字段被要求随输入变化。"""
    assert len(audit_output_variance.MUST_VARY) >= 5, (
        "必变字段表过少，覆盖面不足")
    assert "report.final_score" in audit_output_variance.MUST_VARY
    assert "genome.name" in audit_output_variance.MUST_VARY


def test_fingerprint_distinguishes_values():
    """指纹函数必须能区分不同取值，否则整张表会被判为全恒定。"""
    fp = audit_output_variance._fingerprint
    assert fp({"a": 1}) != fp({"a": 2})
    assert fp("x") != fp("y")
    assert fp([]) != fp(["a"])
