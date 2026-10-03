"""按钮嵌套守卫自身的用例。

为什么要有这一组：守卫脚本失效时，症状是"界面上按钮点了没反应"，
而代码、类型检查、接口验收、页面渲染核查全部通过——只有真人点一次
才会发现。所以守卫自己必须被验证，不能写完就信。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_nested_button.py"


def run(src_dir: Path):
    p = subprocess.run(
        [sys.executable, str(GUARD), str(src_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return p.returncode, p.stdout + p.stderr


def write(d: Path, body: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    f = d / "A.tsx"
    f.write_text(body, encoding="utf-8")
    return f


def test_real_nesting_is_reported(tmp_path):
    """真嵌套必须被报出来，且点名到行。"""
    src = tmp_path / "src"
    write(src, "<button>\n  <button>x</button>\n</button>\n")
    rc, out = run(src)
    assert rc == 1, f"真嵌套未被报出：{out}"
    assert "按钮内又开了按钮" in out


def test_clean_code_passes(tmp_path):
    """平级按钮不得误报。"""
    src = tmp_path / "src"
    write(src, "<div>\n  <button>a</button>\n  <button>b</button>\n</div>\n")
    rc, out = run(src)
    assert rc == 0, f"平级按钮被误报：{out}"


def test_comment_mention_not_reported(tmp_path):
    """注释里提到嵌套不算违规。

    按纯文本扫会把正在说明这条规则的注释判成违规，
    结果守卫在正确实现上变红，而人因此倾向于把它整条删掉。
    """
    src = tmp_path / "src"
    write(src, "// 不得 <button> 里再套 <button>\n<div>\n  <button>a</button>\n</div>\n")
    rc, out = run(src)
    assert rc == 0, f"注释里的说明被误报：{out}"


def test_empty_dir_is_failure(tmp_path):
    """读不到任何源码必须判失败。

    目录为空时"没发现违规"与"全都合规"输出完全一样，
    静默判通过会让整个守卫在路径写错时变成恒真。
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, out = run(empty)
    assert rc == 1, "空目录被判成合规，守卫会退化成恒真"
    assert "没找到任何源码文件" in out


def test_real_frontend_clean():
    """真实前端源码当前必须没有嵌套。"""
    fe = ROOT / "frontend" / "src"
    if not fe.exists():
        pytest.skip("仓库内没有前端源码")
    rc, out = run(fe)
    assert rc == 0, f"真实前端源码存在按钮嵌套：{out}"
