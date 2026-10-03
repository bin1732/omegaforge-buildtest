"""说明文字整洁度守卫的占位：脚本必须真的在跑，且不误报。

## 为什么需要这一层

`scripts/check_comment_hygiene.py` 若只作为独立脚本存在，就没人执行它，
清理成果会在后续改动中悄悄回退。这里把两件事钉住：

1. **真的在跑**：对真实源码做完整扫描，必须退出码 0
2. **真的会失败**：注入内部痕迹后必须退出码 1，否则它是空转
3. **不误报**：正当的技术表述必须放行——误报会引着人去改本来没问题的
   代码，比漏报更危险

校验一律在临时目录做，不触碰工作区源码。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_comment_hygiene.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=240,
    )


def _mkproj(td: str, py_body: str) -> Path:
    """在临时目录搭出最小可扫描的工程结构。"""
    root = Path(td)
    (root / "omegaforge").mkdir()
    (root / "omegaforge" / "probe_mod.py").write_text(py_body, encoding="utf-8")
    return root


# ---------------------------------------------------------------
# 接通：脚本必须存在，且对真实源码扫描通过
# ---------------------------------------------------------------

def test_comment_hygiene_script_passes():
    assert SCRIPT.exists(), f"守卫脚本缺失: {SCRIPT}"
    r = _run(ROOT)
    assert r.returncode == 0, (
        "说明文字中仍有内部痕迹：\n" + (r.stdout or r.stderr)[:2500]
    )


# ---------------------------------------------------------------
# 校验：注入内部痕迹，脚本必须失败
# ---------------------------------------------------------------

@pytest.mark.parametrize(
    "name,body",
    [
        ("过程叙事", '"""模块说明。\n\n此前这里按裸数组解析，运行时恒为空。\n"""\nX = 1\n'),
        ("作业记录", '"""模块说明。\n\n实测：后端返回的是包装对象。\n"""\nX = 1\n'),
        ("章节引用", '"""模块说明。\n\n联调（第43章）：键名是 q。\n"""\nX = 1\n'),
        ("内部术语", '# 守卫会逐条遍历\nX = 1\n'),
        ("构建工具名", '"""模块说明。\n\nCI 会把返回 0 当成执行成功。\n"""\nX = 1\n'),
    ],
)
def test_comment_hygiene_catches_internal_traces(name: str, body: str):
    with tempfile.TemporaryDirectory(prefix="hygiene_rev_") as td:
        r = _run(_mkproj(td, body))
        assert r.returncode == 1, (
            f"注入「{name}」后守卫应当失败，实际退出码 {r.returncode}。"
            "守卫可能已退化为空转。"
        )


# ---------------------------------------------------------------
# 误报对照：正当的技术表述必须放行
# ---------------------------------------------------------------

@pytest.mark.parametrize(
    "name,body",
    [
        ("唯一真源", '"""蒸馏数值边界的唯一真源。\n\n各入口共用同一份，杜绝两边漂移。\n"""\nX = 1\n'),
        ("快照", '# 取快照而非原对象：序列化途中被插入会抛异常。\nX = 1\n'),
        ("旧版本数据兼容", '"""账本可能是旧版本写的，整行不是对象直接跳过。\n"""\nX = 1\n'),
        ("命令行示例中的 grep", '# 刻意不收裸 -c：`grep -c` 是合法常用参数。\nX = 1\n'),
        ("本版为业务概念", '"""本版比上一版本强了多少。\n"""\nX = 1\n'),
    ],
)
def test_comment_hygiene_allows_legitimate_wording(name: str, body: str):
    """正当表述必须放行。

    没有这条对照，词表一旦被塞进通用术语，就会引着人去改本来没问题的
    代码——误报比漏报更危险。
    """
    with tempfile.TemporaryDirectory(prefix="hygiene_allow_") as td:
        r = _run(_mkproj(td, body))
        assert r.returncode == 0, (
            f"「{name}」属正当表述却被误报：\n" + (r.stdout or r.stderr)[:1500]
        )
