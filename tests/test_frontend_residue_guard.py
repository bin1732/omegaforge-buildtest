"""表层残留守卫的占位：脚本必须真的在跑，且真的能抓到。

## 为什么需要这一层

`scripts/check_residue.py` 缺少该约束时只被 CI 工作流引用，**本地回归从不执行**。
后果是：它能报错，但没人看——`SkillsPage.tsx` 里的英文占位提示
`/path/to/skill` 一直存活到被发现为止。这与本项目反复出现的
"契约脚本从不执行"是同一形态：脚本逻辑正确不等于它在生效。

所以这里做两件事：
1. 把脚本接进回归（真实扫描必须退出码 0）
2. 回退校验它**真的会失败**（否则它可能只是一次空转）

回退校验一律在临时目录做，不触碰工作区源码。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_residue.py"
SCAN_ROOT = ROOT / "frontend" / "src"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    [sys.executable, str(SCRIPT), str(root)],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
    timeout=180,
  )


# ---------------------------------------------------------------
# 接通：脚本必须存在，且对真实源码扫描通过
# ---------------------------------------------------------------

def test_residue_script_exists_and_passes():
  """对真实源码做一次完整扫描，必须退出码 0。

  这是"接通"占位：脚本缺少该约束时只挂在 CI 上，本地回归覆盖不到，
  等于没有任何防线。
  """
  assert SCRIPT.exists(), f"守卫脚本缺失: {SCRIPT}"
  assert SCAN_ROOT.is_dir(), f"扫描目录缺失: {SCAN_ROOT}"

  r = _run(SCAN_ROOT)
  assert r.returncode == 0, (
    "存在会被用户看见的开发痕迹：\n" + (r.stdout or r.stderr)[:2000]
  )


# ---------------------------------------------------------------
# 回退校验：注入真实开发痕迹，脚本必须失败
# ---------------------------------------------------------------

@pytest.mark.parametrize(
  "name,snippet",
  [
    (
      "英文占位提示",
      'const A = () => <Input placeholder="/path/to/skill" />\n',
    ),
    (
      "console 调试输出",
      "export const f = () => { console.log('debug here'); return 1 }\n",
    ),
    (
      "TODO 标记",
      "export const g = () => { /* TODO: 临时方案 */ return 2 }\n",
    ),
    (
      "浏览器原生 alert",
      "export const h = () => { alert('出错了') }\n",
    ),
  ],
)
def test_residue_script_catches_real_traces(name: str, snippet: str):
  """注入四类真实开发痕迹，脚本必须逐个抓到并退出码 1。

  只验"通过"是不够的：一个永远返回 0 的脚本同样能让上面那条占位全绿。
  """
  with tempfile.TemporaryDirectory(prefix="residue_rev_") as td:
    root = Path(td)
    (root / "pages").mkdir()
    (root / "pages" / "Probe.tsx").write_text(snippet, encoding="utf-8")

    r = _run(root)
    assert r.returncode == 1, (
      f"注入「{name}」后守卫应当失败，实际退出码 {r.returncode}。"
      "守卫可能已退化为空转。"
    )
    assert "真实残留" in (r.stdout or ""), (
      f"注入「{name}」后输出不像失败报告：{(r.stdout or '')[:600]}"
    )


def test_residue_script_passes_on_clean_tree():
  """干净目录必须退出码 0——证明上面的失败确实由注入内容引起。

  没有这条对照，"退出码 1"可能只是脚本自身崩溃，而非抓到了问题。
  """
  with tempfile.TemporaryDirectory(prefix="residue_clean_") as td:
    root = Path(td)
    (root / "pages").mkdir()
    (root / "pages" / "Clean.tsx").write_text(
      'const A = () => <Input placeholder="示例：/home/用户名/my-skill" />\n',
      encoding="utf-8",
    )
    r = _run(root)
    assert r.returncode == 0, (
      "干净代码被误报，守卫会把人引去改本来没问题的地方：\n"
      + (r.stdout or r.stderr)[:1200]
    )
