"""打包运行时的数据目录守卫。

打包后的后端由桌面外壳拉起，工作目录不可控。若数据目录退回当前目录，
数据就会落进安装目录，而卸载会整棵删除安装目录——用户积累的知识库、
待办、蒸馏产物随之消失，卸载界面不会有任何提示。这里的用例让每一处
判定都有独立证据：撤掉任何一条，对应用例必须变红。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.core import paths  # noqa: E402


def _install_dir(tmp: Path) -> Path:
    d = tmp / "of_install"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_packaged_home_outside_cwd(monkeypatch, tmp_path):
    """打包运行时解析出的数据目录不得落在工作目录内。

    外壳拉起后端时的工作目录可能是安装目录。落进去就等于落进卸载会删
    掉的那棵树，用户数据随之消失。
    """
    install = _install_dir(tmp_path)
    monkeypatch.chdir(install)
    monkeypatch.delenv("OMEGAFORGE_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    home = paths.resolve_home()
    assert os.path.abspath(home) != os.path.abspath(str(install)), (
        "打包运行时数据目录不得等于安装目录；卸载会整棵删除它")
    assert not os.path.abspath(home).startswith(
        os.path.abspath(str(install)) + os.sep), (
        "打包运行时数据目录不得位于安装目录内")


def test_packaged_home_is_user_owned(monkeypatch, tmp_path):
    """打包运行时的数据目录须在用户目录下。

    落在系统目录会静默写入失败：用户存了东西、界面显示成功，重开却什么
    都没有。故必须落到用户有写权限的位置。
    """
    user_root = tmp_path / "user"
    user_root.mkdir()
    monkeypatch.setenv("APPDATA", str(user_root))
    monkeypatch.delenv("OMEGAFORGE_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    home = paths.resolve_home()
    assert os.path.abspath(home).startswith(str(user_root) + os.sep), (
        f"打包运行时数据目录须位于用户目录内，实际：{home}")


def test_env_var_still_wins_when_packaged(monkeypatch):
    """环境变量必须仍然优先于打包默认值。

    否则使用者无法把数据挪到别处，而"改了环境变量没生效"最难排查。
    """
    monkeypatch.setenv("OMEGAFORGE_HOME", "/data/custom-home")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.resolve_home() == "/data/custom-home"


def test_source_run_keeps_cwd_default(monkeypatch, tmp_path):
    """源码运行时仍按工作目录解析。

    便携用法依赖这个默认（把数据带在目录里），改动会波及既有用法与测试。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMEGAFORGE_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.resolve_home() == os.path.join(str(tmp_path), ".omegaforge")


def test_packaged_home_falls_back_to_user_home(monkeypatch):
    """没有 APPDATA / LOCALAPPDATA 时须退回用户主目录。

    两个都读不到就退回当前目录的话，退化成本守卫要拦的那种情形。
    """
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    home = paths.packaged_home()
    assert os.path.abspath(home) != os.getcwd(), (
        "打包默认目录不得退回当前目录")
    assert home.startswith(os.path.expanduser("~")), (
        f"无 APPDATA 时须退回用户主目录，实际：{home}")


def test_no_module_computes_home_locally():
    """数据目录只能由 paths 解析，其他模块不得就地再算一遍。

    就地再算的写法（`os.getcwd()` + `.omegaforge`）在源码运行下与统一解析
    结果一致，所以本地怎么测都对；差别只在打包运行时暴露——统一解析走用户
    目录，就地算得到的是安装目录。于是日志、技能、语音模型这些旁路数据被
    写进安装目录，而该目录不在卸载清单里，卸载后成为无人知晓的孤儿副本。

    因此按字面量扫描：除 paths 之外出现 `.omegaforge` 即报。
    """
    pkg = ROOT / "omegaforge"
    offenders = []
    for f in pkg.rglob("*.py"):
        if f.name == "paths.py":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            offenders.append(f"{f.relative_to(ROOT)}（解析失败）")
            continue
        # 按 AST 取字符串常量：注释里的同名文字不是代码，误报会让本守卫
        # 在正确实现上变红，进而被人整条删掉。
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and ".omegaforge" in node.value:
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
                break
    assert not offenders, (
        "以下模块自行拼出数据目录，须改用 paths.resolve_home()：\n  "
        + "\n  ".join(offenders))
