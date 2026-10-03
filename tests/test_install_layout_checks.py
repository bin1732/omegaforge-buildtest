# -*- coding: utf-8 -*-
"""安装布局校验自身的守卫。

本文件针对 scripts/install_layout_checks.py：那里的每条断言都对应一类
"装完才发现"的失效，而断言若写得恒真，就会永久性地把那类失效放过去。
故每条都配一个反向样本——故意构造失效目录，断言必须报出。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import install_layout_checks as layout  # noqa: E402


def _make(tmp_path, *, app=True, internal=True, runtime=True, frontend=True,
          sidecar=True):
    """构造一个最小安装目录。默认全部合格。"""
    root = tmp_path / "inst"
    root.mkdir()
    # 安装器会把 productName 的空格写成连字符，这里照实模拟
    if app:
        p = root / "omegaforge-studio.exe"
        p.write_bytes(b"MZ" + (b"index.html" + b"bundle.js" + b"assets/app.js") if frontend
                      else b"MZ" + b"\x00" * 64)
    if sidecar:
        (root / "omegaforge-backend.exe").write_bytes(b"MZ" + b"\x00" * 32)
    if internal:
        d = root / "_internal"
        d.mkdir()
        if runtime:
            (d / "python311.dll").write_bytes(b"\x00" * 16)
        (d / "base_library.zip").write_bytes(b"PK\x03\x04")
    return str(root)


def _has(bad: list[str], kw: str) -> bool:
    return any(kw in b for b in bad)


def test_healthy_layout_has_no_problem(tmp_path):
    assert layout.check_layout(_make(tmp_path)) == []


def test_missing_app_exe_is_reported(tmp_path):
    """缺主程序时只验 sidecar 会恒真——用户双击没东西可开。"""
    bad = layout.check_layout(_make(tmp_path, app=False))
    assert _has(bad, "主程序")


def test_missing_internal_is_reported(tmp_path):
    bad = layout.check_layout(_make(tmp_path, internal=False))
    assert _has(bad, "不同级")


def test_empty_internal_is_reported(tmp_path):
    """空目录同样满足"同级"，但里面没有运行时。"""
    root = _make(tmp_path)
    for n in os.listdir(os.path.join(root, "_internal")):
        os.remove(os.path.join(root, "_internal", n))
    bad = layout.check_layout(root)
    assert _has(bad, "为空目录")


def test_missing_python_runtime_is_reported(tmp_path):
    root = _make(tmp_path)
    for n in os.listdir(os.path.join(root, "_internal")):
        if n.startswith("python"):
            os.remove(os.path.join(root, "_internal", n))
    assert _has(layout.check_layout(root), "Python 运行时")


def test_missing_frontend_assets_is_reported(tmp_path):
    """前端没打进去：能启动、接口也通，界面却是全白。"""
    bad = layout.check_layout(_make(tmp_path, frontend=False))
    assert _has(bad, "前端产物")


def test_conf_unreadable_is_not_treated_as_pass(tmp_path):
    """读不到 productName 时不能当成"没有主程序也无所谓"。"""
    orig = layout.CONF
    layout.CONF = str(tmp_path / "nope.json")
    try:
        bad = layout.check_layout(_make(tmp_path))
        assert _has(bad, "读不到 productName")
    finally:
        layout.CONF = orig


def test_frontend_probe_is_not_always_true(tmp_path):
    p = tmp_path / "a.exe"
    p.write_bytes(b"\x00" * 128)
    assert layout.frontend_assets_embedded(str(p)) is False


def test_app_name_matching_tolerates_installer_renaming(tmp_path):
    """安装器把空格写成连字符：按 productName 全文比对会永远找不到主程序，
    而该症状与"产物真的缺主程序"无法区分。"""
    assert layout._norm("OmegaForge Studio") == layout._norm("omegaforge-studio")


@pytest.mark.parametrize("port", [8799])
def test_port_in_use_reports_free_port_as_free(port):
    """未监听的端口必须判为空闲——判反了会让启动前的占用检查恒不成立。"""
    assert layout.port_in_use(port) is False
