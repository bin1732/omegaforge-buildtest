# -*- coding: utf-8 -*-
"""接线守卫自身的守卫。

check_api_wiring.py 的每条判定若写得恒真，就会永久性地把"假功能"
放过去——故每条都配反向样本。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import check_api_wiring as w  # noqa: E402


def test_backend_routes_extracts_known_paths():
    exact, prefixes = w.backend_routes()
    assert "/api/status" in exact
    assert any(p.startswith("/api/jobs") for p in prefixes)


def test_served_rejects_unknown_path():
    """判反了会让"前端调了不存在的路径"永远报不出来。"""
    exact, prefixes = w.backend_routes()
    assert w.served("/api/definitely/not/here", exact, prefixes) is False


def test_frontend_paths_have_no_query_string():
    """查询串必须剥掉：不剥会把真实存在的路由误报成不存在。"""
    for _sym, paths in w.frontend_calls().items():
        for p in paths:
            assert "?" not in p


def test_every_frontend_path_is_served():
    exact, prefixes = w.backend_routes()
    for sym, paths in w.frontend_calls().items():
        for p in paths:
            assert w.served(p, exact, prefixes), f"{sym} -> {p}"


def test_every_export_is_referenced_by_ui():
    used = w.ui_symbols()
    unused = [s for s in w.frontend_calls() if s not in used]
    assert unused == [], f"界面无入口的导出：{unused}"


def test_unused_detection_is_not_always_empty(tmp_path, monkeypatch):
    """构造一个没有页面引用的 api.ts：必须判出未引用，不能恒为空。"""
    api = tmp_path / "api.ts"
    api.write_text(
        "export const ghostCall = () => get('/api/status')\n", encoding="utf-8")
    empty = tmp_path / "pages"
    empty.mkdir()
    monkeypatch.setattr(w, "API_TS", str(api))
    monkeypatch.setattr(w, "PAGES", str(empty))
    monkeypatch.setattr(w, "COMPONENTS", str(tmp_path / "nope"))
    assert "ghostCall" in w.frontend_calls()
    assert "ghostCall" not in w.ui_symbols()


def test_missing_sources_are_not_treated_as_pass(tmp_path, monkeypatch):
    """读不到源码时必须判失败——空集合会让比对恒等成立。"""
    monkeypatch.setattr(w, "SERVER", str(tmp_path / "nope.py"))
    with pytest.raises(SystemExit) as e:
        w.backend_routes()
    assert "读不到" in str(e.value)


def test_extraction_failure_is_not_treated_as_pass(tmp_path, monkeypatch):
    """api.ts 里一个导出都提不到时，不能"没有断头"地通过。"""
    monkeypatch.setattr(w, "API_TS", str(tmp_path / "nope.ts"))
    with pytest.raises(SystemExit) as e:
        w.frontend_calls()
    assert "读不到" in str(e.value)
