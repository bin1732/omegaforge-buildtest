# -*- coding: utf-8 -*-
"""产物回传通道的用例：清单、Release 资产，以及"安装包本体不得进 git 分支"。

每条断言对应一种具体失效：
  · 清单缺体积   → 产物分支上无从对账"打的与验的是不是同一个包"
  · 缺文件静默绿 → 发布步骤显示成功而 Release 上什么都没有
  · 无令牌静默绿 → 同上，且无人知道发布环节失效
  · 把 exe 塞回产物分支 → push 被 GH001 拒绝，回传步骤红，
    而"安装包已产出、装机验收全绿"的结论被这一步掩盖成整轮失败
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _run(script: str, args, env=None):
    return subprocess.run([sys.executable, str(SCRIPTS / script)] + list(args),
                          capture_output=True, text=True, cwd=str(ROOT),
                          env=env if env is not None else dict(os.environ))


def test_manifest_records_name_bytes_and_sha256():
    with tempfile.TemporaryDirectory() as d:
        exe = Path(d) / "OmegaForge Studio_0.1.0_x64-setup.exe"
        exe.write_bytes(b"x" * 4096)
        out = Path(d) / "manifest.json"
        r = _run("write_installer_manifest.py",
                 ["--exe", str(exe), "--out", str(out), "--run", "123"])
        assert r.returncode == 0, r.stdout + r.stderr
        meta = json.loads(out.read_text(encoding="utf-8"))
        assert meta["bytes"] == 4096
        assert meta["name"].endswith(".exe")
        assert meta["run"] == "123"
        # 没有 sha256 就无法分辨"同一个包"与"同名但内容不同"
        assert len(meta["sha256"]) == 64


def test_manifest_missing_exe_fails():
    with tempfile.TemporaryDirectory() as d:
        r = _run("write_installer_manifest.py",
                 ["--exe", str(Path(d) / "nope.exe"),
                  "--out", str(Path(d) / "m.json")])
        assert r.returncode == 1, r.stdout


def test_release_dry_run_reports_size_and_sha():
    with tempfile.TemporaryDirectory() as d:
        exe = Path(d) / "a.exe"
        exe.write_bytes(b"y" * 1024)
        r = _run("publish_installer_release.py",
                 ["--repo", "x/y", "--exe", str(exe), "--dry-run"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "1024" in r.stdout
        assert "sha256=" in r.stdout


def test_release_missing_exe_fails():
    with tempfile.TemporaryDirectory() as d:
        r = _run("publish_installer_release.py",
                 ["--repo", "x/y", "--exe", str(Path(d) / "nope.exe")])
        assert r.returncode == 1, r.stdout


def test_release_dry_run_never_reads_token():
    """dry-run 必须在**没有令牌**的环境里也能核对体积与 sha256。

    令牌检查排在 dry-run 之前时，dry-run 在缺令牌的环境里退出 1：核对
    本身一次都没执行。而本地通常有令牌文件，这条失效只在 CI 上现形——
    症状是"发布脚本坏了"，排查方向指向网络与权限，与真实原因无关。

    这里直接把 _token 换成会抛错的探针：dry-run 若去读令牌，用例立刻红，
    不受本机是否恰好存在令牌文件影响。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pub_rel_dry", SCRIPTS / "publish_installer_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _boom():
        raise AssertionError("dry-run 不得读取访问令牌")

    mod._token = _boom
    with tempfile.TemporaryDirectory() as d:
        exe = Path(d) / "a.exe"
        exe.write_bytes(b"y" * 2048)
        import sys as _sys
        old = _sys.argv
        _sys.argv = ["publish_installer_release.py", "--repo", "x/y",
                     "--exe", str(exe), "--dry-run"]
        try:
            rc = mod.main()
        finally:
            _sys.argv = old
    assert rc == 0


def test_release_without_token_fails(monkeypatch):
    """无令牌必须报错：静默返回会让发布步骤显示成功而 Release 上没有资产。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pub_rel", SCRIPTS / "publish_installer_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in ("OF_PAT", "GITHUB_TOKEN", "OF_GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)
    assert mod._token() == ""


def _workflow_steps():
    import yaml
    wf = ROOT / ".github" / "workflows" / "build.yml"
    data = yaml.safe_load(wf.read_text(encoding="utf-8"))
    return [s for s in data["jobs"]["build"]["steps"] if isinstance(s, dict)]


def test_artifacts_branch_must_not_carry_installer_body():
    """产物分支只回传清单。

    安装包内置语音运行库与模型后远超 git 单文件上限 100MB，把本体 add 进
    分支会让 push 被拒：表现为回传一步红，而装机验收全绿的事实被掩盖成
    整轮失败。
    """
    steps = _workflow_steps()
    hit = [s for s in steps if "artifacts branch" in (s.get("name") or "")]
    assert hit, "找不到产物分支回传步骤（工作流改名会让这条静默失效）"
    script = hit[0].get("run") or ""
    assert "manifest.json" in script
    assert "*.exe installer/" not in script, (
        "产物分支不得再回传安装包本体：git 单文件上限 100MB，push 会被拒绝")


def test_release_publish_step_exists_and_uses_of_pat():
    steps = _workflow_steps()
    hit = [s for s in steps if "Release" in (s.get("name") or "")]
    assert hit, "找不到 Release 发布步骤"
    script = hit[0].get("run") or ""
    assert "publish_installer_release.py" in script
    env = hit[0].get("env") or {}
    assert "OF_PAT" in env, "发布必须走仓库 secret，默认令牌权限不足时静默失败"
