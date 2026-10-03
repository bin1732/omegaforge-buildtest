"""任务列表覆盖范围守卫的用例。

守卫必须能区分"真的合规"与"恒真"：读不到源码时须判失败，
默认 scope 与客户端过滤两种失效形态都须被报出。
任何一条在失效形态下不报错，整条守卫就等于没在查。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_tasks_scope.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_tasks_scope", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _patch(monkeypatch, api_text=None, page_text=None):
    """替换读取内容，使守卫面对指定形态的源码。"""
    def fake_read(p: Path) -> str:
        name = p.name
        if name == "api.ts" and api_text is not None:
            return api_text
        if name == "TasksPage.tsx" and page_text is not None:
            return page_text
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
    monkeypatch.setattr(_load(), "_read", fake_read, raising=True)


def test_current_code_passes(mod):
    assert mod.check() == []


def test_default_scope_is_reported(monkeypatch):
    """改回默认 scope：必须报出来，不能静默通过。"""
    m = _load()
    monkeypatch.setattr(
        m, "_read",
        lambda p: ("export const tasksList = () => "
                   "get('/api/tasks/list')") if p.name == "api.ts"
        else "const visible = showDone ? items : items.filter((t) => !t.done)",
        raising=True)
    bad = m.check()
    assert bad, "默认 scope 必须被报出，否则守卫恒真"
    assert any("scope" in b for b in bad)


def test_client_side_filter_is_reported(monkeypatch):
    """客户端无条件过滤已完成：开关形同虚设，必须报出来。"""
    m = _load()
    monkeypatch.setattr(
        m, "_read",
        lambda p: ("export const tasksList = (scope = 'all') => "
                   "get(`/api/tasks/list?scope=${scope}`)") if p.name == "api.ts"
        else "const visible = items.filter((t) => !t.done)",
        raising=True)
    bad = m.check()
    assert bad, "客户端过滤必须被报出"
    assert any("showDone" in b for b in bad)


def test_unreadable_file_is_reported(monkeypatch):
    """读不到源码不能判通过——否则文件改名后守卫静默失效。"""
    m = _load()
    monkeypatch.setattr(m, "_read", lambda p: "", raising=True)
    bad = m.check()
    assert bad, "读不到内容必须判失败"
    assert any("读不到" in b for b in bad)


def test_missing_definition_is_reported(monkeypatch):
    m = _load()
    monkeypatch.setattr(
        m, "_read",
        lambda p: "" if p.name == "api.ts" else
        "const visible = showDone ? items : items.filter((t) => !t.done)",
        raising=True)
    bad = m.check()
    assert bad
