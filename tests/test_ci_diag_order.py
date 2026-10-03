"""诊断目录顺序守卫的用例。

每条判定都配了反例：撤掉哪一条，对应用例就要变红。
只写"真实工作流通过"不足以证明守卫在查东西 —— 那份通过
在守卫恒真时同样成立。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_ci_diag_order.py"


def _run(path_arg: str | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)]
    if path_arg:
        cmd.append(path_arg)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))


def _wf(body: str) -> str:
    return "jobs:\n  build:\n    steps:\n" + textwrap.indent(textwrap.dedent(body), " " * 6)


def test_real_workflow_passes():
    assert (ROOT / ".github" / "workflows" / "build.yml").is_file()
    r = _run()
    assert r.returncode == 0, r.stdout + r.stderr


def _write(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(_wf(body), encoding="utf-8")
    return str(p)


def test_usage_before_mkdir_is_reported(tmp_path: Path):
    """run79/run80 的真实形态：装运行库排在建立目录之前。"""
    p = _write(
        tmp_path,
        "bad.yml",
        """
        - name: Install voice runtime
          run: |
            set -o pipefail
            python -m pip install sherpa-onnx 2>&1 | tee ci_diag/00b.log
        - name: Prepare diagnostics dir
          run: |
            mkdir -p ci_diag
        """,
    )
    r = _run(p)
    assert r.returncode == 1
    assert "Install voice runtime" in r.stdout


def test_mkdir_after_use_in_same_step_is_reported(tmp_path: Path):
    """同一步内 mkdir 写在使用之后：序号上无法区分，必须按行序判。"""
    p = _write(
        tmp_path,
        "inner.yml",
        """
        - name: Prepare diagnostics dir
          run: |
            echo x > ci_diag/00-env.txt
            mkdir -p ci_diag
        """,
    )
    r = _run(p)
    assert r.returncode == 1
    assert "mkdir 必须排在首次使用" in r.stdout


def test_missing_creator_is_reported(tmp_path: Path):
    """找不到建立目录的步骤时必须判失败 —— 判通过会让守卫恒真。"""
    p = _write(
        tmp_path,
        "nocreator.yml",
        """
        - name: Only usage
          run: |
            python x.py 2>&1 | tee ci_diag/a.log
        """,
    )
    r = _run(p)
    assert r.returncode == 1
    assert "找不到建立" in r.stdout


def test_unreadable_path_is_reported(tmp_path: Path):
    """读不到必须报错，不能静默通过（否则路径写错时守卫永久空转）。"""
    r = _run(str(tmp_path / "nope.yml"))
    assert r.returncode == 1
    assert "读不到工作流文件" in r.stdout


def test_comment_lines_are_not_reported(tmp_path: Path):
    """注释里提到 ci_diag/ 不算违规 —— 否则守卫会在正确实现上变红。"""
    p = _write(
        tmp_path,
        "comment.yml",
        """
        - name: Prepare diagnostics dir
          run: |
            mkdir -p ci_diag
        - name: Later
          run: |
            # 说明：日志落在 ci_diag/00-x.log
            echo ok
        """,
    )
    r = _run(p)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("bad", ["", "jobs: null\n", "steps: 3\n"])
def test_unparsable_or_empty_is_reported(tmp_path: Path, bad: str):
    p = tmp_path / "bad2.yml"
    p.write_text(bad, encoding="utf-8")
    r = _run(str(p))
    assert r.returncode == 1
