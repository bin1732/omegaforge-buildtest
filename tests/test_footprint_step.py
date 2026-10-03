"""装机体积上报步骤的行为校验。

说明文字口径同 tests/ 下其余用例：只写约束，不写开发过程。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_footprint_step as cfs  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/build.yml"


def _write(tmp: Path, text: str) -> Path:
    path = tmp / "build.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _strip_guard(src: str) -> str:
    """去掉目录存在性判定与管道容错，得到会被本校验点名的写法。"""
    match = re.search(
        r"[ \t]*if \[ ! -d /c/of_install \]; then.*?\n[ \t]*fi\n", src, re.S)
    assert match is not None, "未定位到目录存在性判定"
    keep = (
        '            echo "installed_files=$(find /c/of_install -type f '
        '2>/dev/null | wc -l)"\n'
        '            echo "installed_MB=$(du -sm /c/of_install 2>/dev/null '
        '| cut -f1)"\n'
    )
    out = src[:match.start()] + keep + src[match.end():]
    out = out.replace(' || echo "（未产出安装包）"', "")
    out = out.replace(' || echo "（安装目录不存在）"', "")
    assert 'echo "未采集' not in out, "存在性判定未移除"
    assert "（未产出安装包）" not in out, "管道容错未移除"
    return out


def test_real_workflow_passes():
    problems = cfs.check(WORKFLOW)
    assert problems == [], problems


def test_stripped_guard_is_named():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), _strip_guard(
            WORKFLOW.read_text(encoding="utf-8")))
        problems = cfs.check(path)
    joined = "\n".join(problems)
    # 两条都要点名：只认其中一条时，另一条失效会被当成通过。
    assert "installed_files=0" in joined, joined
    assert "exit 0 未生效" in joined or "未报「未采集」" in joined, joined


def test_stripped_guard_exit_code_is_not_zero():
    """无容错写法在目录缺失时必须非 0：exit 0 到不了，步骤被判失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), _strip_guard(
            WORKFLOW.read_text(encoding="utf-8")))
        rc, out = cfs.run_script(cfs.find_step_run(path), Path(tmp))
    assert rc != 0, f"退出码为 {rc}，与预期相反：{out[:300]}"


def test_missing_step_is_not_silently_passed():
    """定位不到步骤必须判失败：返回空列表会让本校验在改名后恒为通过。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), "name: x\non: push\njobs:\n  build:\n    steps: []\n")
        problems = cfs.check(path)
    assert problems and "未" in "\n".join(problems), problems


def test_extracted_script_contains_dir_literal():
    script = cfs.find_step_run(WORKFLOW)
    assert script is not None
    assert cfs.INSTALL_DIR in script
    assert script.rstrip().endswith("exit 0")


def test_variant_without_literal_is_named():
    """字面量缺失时场景无从构造，必须点名而不是跳过。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), _strip_guard(
            WORKFLOW.read_text(encoding="utf-8")
        ).replace("/c/of_install", "/c/other_place"))
        problems = cfs.check(path)
    joined = "\n".join(problems)
    assert "替换目录字面量未生效" in joined, joined


def test_dir_comes_from_env_not_from_text():
    """目录必须经环境传入：写进脚本文本会被 bash 按词法转义。"""
    script = cfs.find_step_run(WORKFLOW)
    assert script is not None
    variant = script.replace(cfs.INSTALL_DIR, '"$' + cfs.DIR_ENV + '"')
    assert cfs.INSTALL_DIR not in variant
    assert cfs.DIR_ENV in variant
    # Windows 路径写进 bash 脚本文本时，反斜杠被当成转义符吃掉，采集到的是
    # 另一个目录。故替换后不得出现反斜杠。
    assert chr(92) not in variant, variant


def test_dir_with_space_is_surveyed():
    """目录名含空格时仍须报出真实数字：验证值不经词法解析。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        populated = root / "in stall"
        populated.mkdir()
        for k in range(3):
            (populated / f"f{k}.bin").write_bytes(b"x" * 2048)
        script = cfs.find_step_run(WORKFLOW)
        variant = script.replace(cfs.INSTALL_DIR, '"$' + cfs.DIR_ENV + '"')
        env = os.environ.copy()
        env[cfs.DIR_ENV] = str(populated)
        rc, out = cfs.run_script(variant, root, env=env)
    assert rc == 0, out[:300]
    assert "installed_files=3" in out, out[:300]
