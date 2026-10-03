"""数据目录环境变量名守卫自身的守卫。

这一层最坏的不是失效，是**看着在查、实际一条都没报**：判定分支若被
continue 之类的控制流吃掉，扫描照常跑完、退出码照常是 0，与全仓合规
完全无法区分。故三条用例各自钉住一个方向，撤掉任一条必须变红。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_home_env_name.py"


def _run() -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(GUARD)], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_clean_repo_passes():
    """当前仓库必须判通过——前提不成立的话其余三条都无意义。"""
    rc, out = _run()
    assert rc == 0, f"守卫在干净仓库上报错：{out[:400]}"


def test_wrong_name_is_caught():
    """写错名字必须点名到行号，不得静默放过。"""
    target = ROOT / "scripts" / "verify_pages_browser.py"
    src = target.read_text(encoding="utf-8")
    needle = 'env["OMEGAFORGE_HOME"]'
    assert needle in src, "找不到设置点，用例前提已失效，须重定位"
    try:
        target.write_text(src.replace(needle, 'env["OF_HOME"]', 1),
                          encoding="utf-8")
        rc, out = _run()
        assert rc != 0, "写错的环境变量名未被报出：这一层什么都没查到"
        assert "OF_HOME" in out, f"未点名是哪个名字错了：{out[:400]}"
        assert "verify_pages_browser.py" in out, (
            f"未指出位置：{out[:400]}")
    finally:
        target.write_text(src, encoding="utf-8")
    assert _run()[0] == 0, "还原后仍未通过，用例可能污染了仓库"


def test_env_name_is_read_from_paths():
    """环境变量名必须取自 paths.py，不得在守卫里写死。

    写死之后 paths.py 改名会让所有设置点同时失效，而守卫照绿。
    """
    src = GUARD.read_text(encoding="utf-8")
    assert "read_env_name" in src and "_ENV" in src, (
        "守卫没有从 paths.py 读 _ENV：改名后会整层静默失效")
    assert '"OMEGAFORGE_HOME"' not in src.split("def scan")[0], (
        "守卫开头写死了环境变量名：paths.py 改名时它不会发现")
