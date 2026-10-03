"""卸载后本进程自行退出的守卫。

卸载器不终止后台进程时，用户以为卸掉了，进程却仍在占端口、仍在写数据。
本模块守住"靠同目录其他可执行文件消失来判定已卸载"这条机制，且每一条
判定都有独立证据——撤掉任何一处，对应用例必须变红。
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _server():
    # 该模块用相对导入，必须按包导入，不能按文件路径加载
    import omegaforge.server as m
    return m


class _FakeHttp:
    def __init__(self):
        self.calls = 0

    def shutdown(self):
        self.calls += 1


def _prep(tmp: str, with_sibling: bool = True):
    """造一个安装目录，并把 sys 伪装成打包运行在该目录里的后端程序。"""
    me = os.path.join(tmp, "omegaforge-backend.exe")
    with open(me, "wb") as f:
        f.write(b"x")
    if with_sibling:
        with open(os.path.join(tmp, "omegaforge-studio.exe"), "wb") as f:
            f.write(b"x")
    old_exe, old_frozen = sys.executable, getattr(sys, "frozen", False)
    sys.executable = me
    sys.frozen = True
    return me, old_exe, old_frozen


def test_baseline_excludes_self():
    """基线不得包含自身。

    自身文件在运行中被占用，卸载器删不掉它；把它算进基线的话，"自身还
    在"永远成立，守卫永远不会触发，等于没有守卫。
    """
    m = _server()
    with tempfile.TemporaryDirectory() as d:
        me, oexe, ofrozen = _prep(d)
        try:
            base = m._sibling_exe_baseline()
            assert base, "有同级主程序时基线不得为空"
            assert os.path.abspath(me) not in {os.path.abspath(p) for p in base}
        finally:
            sys.executable, sys.frozen = oexe, ofrozen


def test_guard_disabled_when_baseline_empty():
    """基线为空时不得启用守卫。

    安装目录里本就没有其他可执行文件的话，"基线全部消失"恒为真，一启动
    就会自杀——用户刚打开应用它就没了。宁可不监控，也不可误退。
    """
    m = _server()
    with tempfile.TemporaryDirectory() as d:
        me, oexe, ofrozen = _prep(d, with_sibling=False)
        try:
            assert not m._sibling_exe_baseline(), "无同级可执行文件时基线须为空"
            assert m._start_uninstall_watch(_FakeHttp(), 0.2) is None, (
                "基线为空时必须放弃监控；启用会导致启动即自杀")
        finally:
            sys.executable, sys.frozen = oexe, ofrozen


def test_guard_disabled_when_not_frozen():
    """源码运行时不得启用。

    源码运行没有安装目录可言，启用会把开发时的目录变动当成卸载。
    """
    m = _server()
    with tempfile.TemporaryDirectory() as d:
        me, oexe, ofrozen = _prep(d)
        try:
            sys.frozen = False
            assert m._start_uninstall_watch(_FakeHttp(), 0.2) is None
        finally:
            sys.executable, sys.frozen = oexe, ofrozen


def test_shutdown_fires_when_all_siblings_gone():
    """基线全部消失时必须关闭服务。

    这正是卸载后的真实形态：主程序已被删掉，后端进程还活着。
    """
    m = _server()
    with tempfile.TemporaryDirectory() as d:
        me, oexe, ofrozen = _prep(d)
        try:
            httpd = _FakeHttp()
            t = m._start_uninstall_watch(httpd, 0.2)
            assert t is not None
            time.sleep(0.4)
            assert httpd.calls == 0, "基线仍在时不得退出——否则正常运行会自杀"
            os.remove(os.path.join(d, "omegaforge-studio.exe"))
            for _ in range(40):
                if httpd.calls:
                    break
                time.sleep(0.1)
            assert httpd.calls >= 1, (
                "同级可执行文件全部消失后必须关闭服务；不关就等于卸载后残留")
        finally:
            sys.executable, sys.frozen = oexe, ofrozen


def test_baseline_excludes_uninstaller():
    """卸载器自身不得进入基线。

    静默卸载时卸载器不会删除自己。把它算进基线的话，"全部消失"永远不
    成立，守卫一次都不会触发——看着装了守卫，实际与没装一样。
    """
    m = _server()
    with tempfile.TemporaryDirectory() as d:
        me, oexe, ofrozen = _prep(d)
        try:
            with open(os.path.join(d, "Uninstall.exe"), "wb") as f:
                f.write(b"x")
            base = {os.path.basename(p).lower() for p in m._sibling_exe_baseline()}
            assert "uninstall.exe" not in base, (
                "卸载器须排除出基线；否则它残留会让守卫永不触发")
        finally:
            sys.executable, sys.frozen = oexe, ofrozen


def test_shutdown_fires_even_if_uninstaller_remains():
    """卸载器残留时仍须退出。

    这是静默卸载的真实形态：主程序已被删掉，卸载器自己还在目录里。
    """
    m = _server()
    with tempfile.TemporaryDirectory() as d:
        me, oexe, ofrozen = _prep(d)
        try:
            with open(os.path.join(d, "Uninstall.exe"), "wb") as f:
                f.write(b"x")
            httpd = _FakeHttp()
            t = m._start_uninstall_watch(httpd, 0.2)
            assert t is not None
            os.remove(os.path.join(d, "omegaforge-studio.exe"))
            for _ in range(40):
                if httpd.calls:
                    break
                time.sleep(0.1)
            assert httpd.calls >= 1, (
                "卸载器残留不应阻止退出；不退出即卸载后进程仍占端口")
        finally:
            sys.executable, sys.frozen = oexe, ofrozen


def test_watch_runs_in_background():
    """监控不得阻塞服务主循环，否则监听根本起不来。"""
    m = _server()
    src = inspect.getsource(m._start_uninstall_watch)
    assert "daemon=True" in src, "监控线程须为守护线程，不得拖住进程退出"
    assert "Thread(" in src, "监控须在独立线程中进行，不得阻塞 serve_forever"
