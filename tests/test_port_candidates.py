"""候选端口守卫的用例。

含失效形态：两侧不一致、任一侧缺失、候选为空。空结果必须判失败——
读不到内容时若返回"通过"，守卫会在路径写错时退化成恒真。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_port_candidates.py"


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_real_repo_passes():
    r = _run(ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_mismatch_is_reported(tmp_path: Path):
    (tmp_path / "omegaforge").mkdir()
    (tmp_path / "frontend" / "src" / "lib").mkdir(parents=True)
    (tmp_path / "omegaforge" / "server.py").write_text(
        "PORT_CANDIDATES: tuple[int, ...] = (8787, 8788)\n", encoding="utf-8"
    )
    (tmp_path / "frontend" / "src" / "lib" / "api.ts").write_text(
        "const PORT_CANDIDATES = [8787, 8788, 8789]\n", encoding="utf-8"
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "不一致" in r.stdout


def test_missing_side_is_reported(tmp_path: Path):
    (tmp_path / "omegaforge").mkdir()
    (tmp_path / "omegaforge" / "server.py").write_text(
        "PORT_CANDIDATES: tuple[int, ...] = (8787,)\n", encoding="utf-8"
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "界面" in r.stdout


def test_empty_candidates_is_reported(tmp_path: Path):
    (tmp_path / "omegaforge").mkdir()
    (tmp_path / "frontend" / "src" / "lib").mkdir(parents=True)
    (tmp_path / "omegaforge" / "server.py").write_text(
        "PORT_CANDIDATES: tuple[int, ...] = ()\n", encoding="utf-8"
    )
    (tmp_path / "frontend" / "src" / "lib" / "api.ts").write_text(
        "const PORT_CANDIDATES: number[] = []\n", encoding="utf-8"
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "为空" in r.stdout


def test_comment_mention_is_not_a_definition(tmp_path: Path):
    """注释里的同形文字不算定义；只有真实字面量参与判定。"""
    (tmp_path / "omegaforge").mkdir()
    (tmp_path / "frontend" / "src" / "lib").mkdir(parents=True)
    (tmp_path / "omegaforge" / "server.py").write_text(
        "# PORT_CANDIDATES = (1, 2)\nPORT_CANDIDATES: tuple[int, ...] = (8787,)\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "src" / "lib" / "api.ts").write_text(
        "// PORT_CANDIDATES = [9, 9]\nconst PORT_CANDIDATES = [8787]\n",
        encoding="utf-8",
    )
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout
    assert "8787" in r.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
