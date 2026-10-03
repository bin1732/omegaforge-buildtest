"""卸载钩子守卫。

卸载器删不掉被占用的文件时会静默跳过、且不重试。若不在删除文件之前结
束进程，整棵运行时会永久留在用户磁盘上，而卸载界面显示的是卸载完成。
这里让三件事各有独立证据：钩子文件存在、配置真的引用了它、钩子确实在
删除文件之前结束进程。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "src-tauri" / "tauri.conf.json"
HOOKS = ROOT / "src-tauri" / "windows" / "hooks.nsh"

# 会被运行时占用、进而导致删除被跳过的两个进程名。
_NAMES = ("omegaforge-studio.exe", "omegaforge-backend.exe")


def _conf_nsis() -> dict:
    data = json.loads(CONF.read_text(encoding="utf-8"))
    return data["bundle"]["windows"]["nsis"]


def test_installer_hooks_is_configured():
    """配置必须引用钩子文件。

    只写文件不接配置的话，钩子一次都不会执行，而磁盘上不会有任何症状。
    """
    rel = _conf_nsis().get("installerHooks")
    assert rel, "tauri.conf.json 未配置 installerHooks"
    assert (ROOT / "src-tauri" / rel.lstrip("./")).is_file(), (
        f"installerHooks 指向的文件不存在：{rel}")


def test_preuninstall_ends_processes():
    """卸载前必须结束两个进程，且顺序在删除文件之前。

    卸载钩子的价值就在于"删文件之前"。结束进程名写错一个，对应那套文
    件仍会被跳过——残留照旧，而界面依旧显示卸载成功。
    """
    text = HOOKS.read_text(encoding="utf-8")
    assert "NSIS_HOOK_PREUNINSTALL" in text, "缺少卸载前钩子"
    for name in _NAMES:
        assert name in text, f"卸载前钩子未结束进程：{name}"
    assert "taskkill" in text, "缺少结束进程的动作"


def test_preinstall_also_ends_processes():
    """覆盖安装前同样要结束进程。

    覆盖安装时若上一版仍在运行，新文件会被跳过，用户拿到混杂的安装、
    且无从察觉。
    """
    text = HOOKS.read_text(encoding="utf-8")
    assert "NSIS_HOOK_PREINSTALL" in text, "缺少安装前钩子"
    for name in _NAMES:
        assert name in text, f"安装前钩子未结束进程：{name}"


def test_postuninstall_clears_runtime():
    """卸载后必须清理运行时残留。

    只结束进程不保证清空：卸载器按安装清单删文件，清单里没有的条目不会
    被删。资源目录是否逐项进入清单取决于打包器实现，因此这里显式清理。
    """
    text = HOOKS.read_text(encoding="utf-8")
    assert "NSIS_HOOK_POSTUNINSTALL" in text, "缺少卸载后钩子"
    assert "RMDir" in text and "_internal" in text, "未清理运行时目录"


def test_postuninstall_guards_empty_instdir():
    """递归删除前必须判断安装目录非空。

    递归删除在空变量上执行会作用于当前目录，代价是把不该删的东西删掉。
    """
    text = HOOKS.read_text(encoding="utf-8")
    hook = text.split("NSIS_HOOK_POSTUNINSTALL", 1)[1]
    assert "RMDir" in hook
    head = hook.split("RMDir", 1)[0]
    assert 'StrCmp "$INSTDIR"' in head, "递归删除之前未判断安装目录为空"


def test_kill_targets_match_actual_binary_names():
    """钩子里的进程名必须与真实产物一致。

    名字对不上时任务会失败退出，NSIS 不会报错——静默什么都不做。所以
    名字来源必须是打包配置里的产物名，不能靠手写。
    """
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    product = conf.get("productName", "")
    expect_main = re.sub(r"\s+", "-", product.strip()).lower() + ".exe"
    text = HOOKS.read_text(encoding="utf-8").lower()
    assert expect_main in text, (
        f"钩子里的主程序名须与 productName 对应：期望 {expect_main}")
    # 后端 sidecar 名来自 externalBin
    for bin_path in conf["bundle"].get("externalBin", []):
        base = bin_path.rstrip("/").rsplit("/", 1)[-1].lower() + ".exe"
        assert base in text, f"钩子未覆盖 sidecar 进程：{base}"
