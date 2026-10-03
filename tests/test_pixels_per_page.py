"""逐页像素校验自身的守卫。

这一层最容易退化成两种形态，两者都不报错，只表现为"通过"：

 · 缺图跳过 —— 某页截不出图时静默少一张，报告仍写全部通过，
   而缺的往往正是白屏那一页
 · 判定恒真 —— 阈值写反或图源错，纯白页也判有内容

两条各有独立反例：撤掉任一条，对应用例必须变红。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_pixels_per_page import analyze_shots, EXPECTED_PAGES  # noqa: E402


def _blank(d: Path, label: str) -> None:
    from PIL import Image
    Image.new("RGB", (200, 120), (255, 255, 255)).save(d / f"{label}.png")


def test_blank_page_is_caught(tmp_path):
    """纯白页必须判失败——否则整层恒真。"""
    for lb in EXPECTED_PAGES:
        _blank(tmp_path, lb)
    _, fails = analyze_shots(EXPECTED_PAGES, str(tmp_path))
    assert fails, "全白页被判有内容：这一层什么都没验到"


def test_missing_shot_is_not_skipped(tmp_path):
    """缺一页必须点名，不得静默放行。"""
    for lb in EXPECTED_PAGES[:-1]:
        _blank(tmp_path, lb)
    _, fails = analyze_shots(EXPECTED_PAGES, str(tmp_path))
    miss = [f for f in fails if "缺截图" in f]
    assert len(miss) == 1, f"缺图未被点名（实际 {len(miss)} 条）：{miss}"
    assert EXPECTED_PAGES[-1] in miss[0], f"未报出缺的是哪一页：{miss}"


def test_empty_dir_fails(tmp_path):
    """空目录须判失败，不得当作全部通过。"""
    _, fails = analyze_shots(EXPECTED_PAGES, str(tmp_path))
    assert len(fails) == len(EXPECTED_PAGES), (
        f"空目录只报 {len(fails)} 条，缺图被静默放行")


def test_expected_pages_count():
    """页面清单不得为空——空清单会让上述三条全部通过而什么都没查。"""
    assert len(EXPECTED_PAGES) >= 10, (
        f"页面清单不足 10 页：{EXPECTED_PAGES}")
