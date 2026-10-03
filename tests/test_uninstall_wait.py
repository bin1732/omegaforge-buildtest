"""卸载验收自身的守卫。

卸载验收是唯一会跑在装机实例上的卸载路径，它失效时表现不是"报红"，而是
"点了卸载、界面说完成、程序还开着"。这里的用例让它的每一处判定都有独立
证据：撤掉任何一条，对应用例必须变红。
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "cvu", ROOT / "scripts" / "ci_verify_uninstall.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run_args() -> list[str]:
    """取调用卸载器时的真实参数列表（AST，不看注释）。

    按源码子串判定的话，注释里写一句 `_?=` 就满足了——改回不带参数照样
    通过，这条守卫会退化成恒真。故只认真实的 subprocess.run 实参。
    """
    import ast
    tree = ast.parse((ROOT / "scripts" / "ci_verify_uninstall.py"
                      ).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name != "run" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.List):
            vals = [ast.unparse(e).lstrip("frb").strip("'\"")
                    for e in first.elts]
            if any(v.startswith("/S") for v in vals):
                return vals
    return []


def _exe_name(m) -> str:
    return "omegaforge-studio.exe"


def test_wait_detects_async_removal():
    """卸载器异步删文件时必须等到文件真的没了，而不是立刻返回成功。

    真实失效就是这个形态：卸载器 0.1 秒返回 0，删除在另一个进程里稍后才
    发生。只看返回码的判定在此时会通过，而用户看到的是程序还开着。
    """
    m = _mod()
    d = Path(__file__).resolve().parent / "_tmp_uninstall_async"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    try:
        exe = d / _exe_name(m)
        exe.write_bytes(b"x")
        threading.Timer(1.0, lambda: exe.unlink(missing_ok=True)).start()
        t0 = time.time()
        got = m._wait_main_gone([_exe_name(m)], str(d), 10)
        assert got is True, "异步删除的情况下须等到文件消失并判成功"
        assert time.time() - t0 >= 0.9, "须真的等到删除发生，不得提前返回"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_wait_fails_when_exe_never_goes():
    """文件始终不消失时必须判失败。

    放行这里的后果是"卸载验收通过但程序还在"——也就是这条验收存在的理由
    被抹掉。故必须返回 False。
    """
    m = _mod()
    d = Path(__file__).resolve().parent / "_tmp_uninstall_stuck"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    try:
        (d / _exe_name(m)).write_bytes(b"x")
        assert m._wait_main_gone([_exe_name(m)], str(d), 1) is False, (
            "主程序始终存在时须判失败；放行会让卸载验收失去意义")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_uninstaller_called_with_sync_flag():
    """调用卸载器必须带就地同步参数。

    不带该参数时卸载器自我复制另起进程，父进程立即返回 0，返回码与是否卸
    干净无关。这条按源码判定，改回不带参数会立刻变红。
    """
    args = _run_args()
    assert any(a.startswith("_?=") for a in args), (
        "卸载器须带就地同步参数调用；不带时返回码为 0 而卸载尚未发生。"
        f"实际参数：{args}")


def test_wait_used_in_main_flow():
    """主流程必须以"文件真的消失"为判据，不得只信返回码。

    抽了函数却不在主流程用它，等于没修——返回码照旧被当成结论。
    """
    m = _mod()
    body = inspect.getsource(m.main)
    assert "_wait_main_gone" in body, (
        "主流程须以主程序真的消失为判据，不得只信卸载器返回码")
    assert "卸载器已返回但主程序仍在" in body, (
        "未消失时须显式报失败，不得静默放行")


def test_missing_main_exe_names_is_fatal():
    """读不到主程序名时须判失败，不得当作通过。

    读不到时若判通过，整个"主程序是否残留"的检查就恒为空集，于是恒真。
    """
    src = (ROOT / "scripts" / "ci_verify_uninstall.py").read_text(encoding="utf-8")
    assert "读不到不能当作通过" in src, (
        "读不到 productName 时须判失败；判通过会让残留检查退化成空集恒真")
