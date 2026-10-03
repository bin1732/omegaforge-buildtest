# -*- coding: utf-8 -*-
"""发布产物完整性守卫：源码看起来全有的东西，装出来也必须真的有。

## 为什么需要这个文件

在源码目录里跑测试，测的是源码树；用户拿到的是打包产物。两者可以
完全分叉：源码数百个文件齐全，而构建出的 wheel 只含 dist-info、名为
UNKNOWN-0.0.0、体积不到 2KB——pip install 之后 omegaforge 命令根本不
存在。这类分叉在源码层做任何检查都发现不了，因为源码本身没有问题，
分叉只发生在打包这一步。

因此本文件的守卫对着**产物**断言，而不是对着源码断言。

## 三条口径

  1. 构建后端已声明（否则 setuptools 回退旧行为，产物是空壳）
  2. 包发现范围限定为 omegaforge*（否则顶层 tests / probes / scripts
     会被一并收进发布包）
  3. 构建出的 wheel 里真的有 omegaforge 模块且入口点存在

第 3 条是唯一能直接证伪"空壳包"的一条：前两条看的是配置声明，声明
对了产物仍可能为空，只有打开产物清点才能区分。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10 及以下没有标准库 tomllib
    import tomli as tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")


def _read_pyproject() -> dict:
    with open(PYPROJECT, "rb") as f:
        return tomllib.load(f)


def _find_include(doc: dict) -> str:
    return (doc.get("tool", {})
               .get("setuptools", {})
               .get("packages", {})
               .get("find", {})
               .get("include", ""))


def test_build_backend_declared():
    """缺 [build-system] 时产物退化为名为 UNKNOWN 的空壳包。"""
    doc = _read_pyproject()
    bs = doc.get("build-system")
    assert bs, (
        "pyproject.toml 缺少 [build-system]：setuptools 会回退旧行为，"
        "既不读 [project] 元数据也不自动发现包，产物是空壳 wheel")
    requires = str(bs.get("requires", "")).lower()
    assert "setuptools" in requires, (
        f"[build-system].requires 未声明 setuptools：{requires!r}")
    backend = str(bs.get("build-backend", ""))
    assert "setuptools" in backend, (
        f"build-backend 不是 setuptools 系：{backend!r}")


def test_setuptools_version_floor_supports_project_table():
    """[project] 元数据需要 setuptools>=61 才被识别。"""
    doc = _read_pyproject()
    requires = str(doc["build-system"].get("requires", ""))
    digits = "".join(ch for ch in requires if ch.isdigit())
    floor = int(digits[:2]) if len(digits) >= 2 else 0
    assert floor >= 61, (
        f"setuptools 版本下限低于 61，[project] 表不会被读取：{requires!r}")


def test_package_discovery_scoped_to_omegaforge():
    """扁平布局下自动发现会把 tests / probes / scripts 收进发布包。"""
    include = _find_include(_read_pyproject())
    assert include, (
        "未限定包发现范围：顶层 tests / probes / scripts / frontend "
        "会被当作包一并打进发布产物")
    joined = " ".join(include) if isinstance(include, list) else str(include)
    assert "omegaforge" in joined, f"包发现范围不含 omegaforge：{include!r}"


def test_project_metadata_not_unknown():
    """空壳包的元数据里 Name / Version 会退化为 UNKNOWN / 0.0.0。"""
    doc = _read_pyproject()
    proj = doc.get("project", {})
    assert proj.get("name") == "omegaforge", (
        f"包名不是 omegaforge：{proj.get('name')!r}")
    assert str(proj.get("version", "")).strip(), "version 不得为空"
    assert (proj.get("scripts", {}) or {}).get("omegaforge"), (
        "未声明 omegaforge 控制台入口")


def _build_wheel(outdir: str):
    # 保留 PYTHONPATH：构建入口本身装在依赖目录里，清掉之后子进程连
    # build 都导入不了，失败的理由会写成"构建未产出 wheel"——那会把
    # 环境缺件读成产物缺陷，排查方向直接带偏。
    return subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", outdir],
        cwd=ROOT, capture_output=True, text=True, timeout=600)


def _build_env_setuptools() -> str:
    """构建子进程实际会用的 setuptools 版本。

    必须在子进程里问：测试进程自己 import 到的版本来自另一套路径，
    说明不了构建时用的是哪个。低于 61 时 [project] 表不会被读取。
    """
    r = subprocess.run(
        [sys.executable, "-c", "import setuptools;print(setuptools.__version__)"],
        capture_output=True, text=True, timeout=120)
    return (r.stdout or "").strip()


def _ver_tuple(text: str):
    nums = []
    for part in str(text).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums[:2] + [0] * (2 - len(nums[:2])))


def test_wheel_really_contains_omegaforge(tmp_path):
    """打开产物清点：空壳包在这里必须变红。

    只断言配置声明不够——声明正确而产物为空是同一处缺陷的另一种表现
    形态，只有清点 wheel 内容才能区分。
    """
    try:
        import build  # noqa: F401
    except ImportError:
        pytest.skip("环境缺件：未安装 build，真实构建校验在 CI 上执行")
    ver = _build_env_setuptools()
    if _ver_tuple(ver) < (61, 0):
        pytest.skip(f"环境缺件：构建环境的 setuptools 为 {ver!r}，低于 61，"
                    f"[project] 表不会被读取，产物必然退化为 UNKNOWN 空壳。"
                    f"这是环境限制而非产物缺陷，真实构建校验在 CI 上执行。")
    r = _build_wheel(str(tmp_path))
    wheels = [f for f in os.listdir(str(tmp_path)) if f.endswith(".whl")]
    assert wheels, (
        f"构建未产出 wheel（构建环境 setuptools={ver!r}）："
        f"{r.stdout[-800:]}{r.stderr[-800:]}")

    name = wheels[0]
    assert name.startswith("omegaforge"), (
        f"产物名不是 omegaforge（空壳包的特征是 UNKNOWN）：{name}")

    with zipfile.ZipFile(os.path.join(str(tmp_path), name)) as z:
        names = z.namelist()
        mods = [n for n in names if n.startswith("omegaforge/")]
        assert mods, (
            f"wheel 里没有任何 omegaforge 模块（空壳包）：共 {len(names)} 个条目")
        assert any(n.endswith("cli.py") for n in mods), (
            "wheel 缺少 omegaforge/cli.py —— 入口点指向的模块不存在")

        # 包发现范围失控时，顶层 tests / probes / scripts 会被一并收进
        # 发布产物。它不像空壳那样让命令不存在，而是把内部脚本发给
        # 用户——源码层的检查看不见，只有打开产物才看得到。
        foreign = sorted({n.split("/")[0] for n in names
                          if n.split("/")[0] not in ("omegaforge",)
                          and not n.split("/")[0].endswith(".dist-info")})
        assert not foreign, (
            f"wheel 混入了非发布内容：{foreign} —— 包发现范围未限定为 "
            f"omegaforge*")

        dist = [n for n in names if n.endswith("dist-info/entry_points.txt")]
        assert dist, "wheel 缺少 entry_points.txt —— 命令不会被安装"
        ep = z.read(dist[0]).decode("utf-8", errors="replace")
        assert "omegaforge" in ep, f"入口点未声明 omegaforge 命令：{ep!r}"


def test_wheel_guard_is_executed_in_ci():
    """沙盒里真实构建那条会跳过，必须确认 CI 上真的会跑。

    跳过的风险是：环境长期不满足时这些守卫永远不执行，而"跳过"在汇总
    行上与"通过"同样不显示为失败。因此本条对着工作流断言——构建入口与
    版本下限确实已在 CI 上备齐，跳过不会退化为永久盲区。
    """
    wf = os.path.join(ROOT, ".github", "workflows", "build.yml")
    assert os.path.exists(wf), f"找不到工作流文件：{wf}"
    with open(wf, encoding="utf-8") as f:
        text = f.read()
    assert "pytest tests/" in text, (
        "工作流没有跑 tests/：本文件的守卫在 CI 上不会被执行")
    assert "pip install -q build" in text, (
        "CI 未安装 build：真实构建校验会永久跳过，空壳包无从发现")
    assert "setuptools>=61" in text, (
        "CI 未把 setuptools 升到 61 以上：[project] 表不会被读取，"
        "产物退化为 UNKNOWN 空壳包")
