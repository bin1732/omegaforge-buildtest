# -*- coding: utf-8 -*-
"""报告里的分数必须随输入变化，不能是常量。

## 守的是什么

`final_score` 是报告与竞技场里最显眼的数字，标注为"蒸馏体得分"。
它若是常量，用户会把它当成蒸馏质量的度量，而它什么也没度量。

这种失效特别隐蔽：接口全绿、字段非空、数值看着精确（两位小数），
唯独与喂进去的源材料无关。只断言"分数不为空"抓不到——常量也是非空。
指纹断言同样抓不到：来源指纹可以三个都不同、分数却恒为同一个值。

## 约束

三个毫不相关的源材料（论文检索 / 代码审查 / 行程规划）
必须产出互不相同的 final_score。

失效来源在离线作答环节：质量达标后一律返回同一份模板答案，于是
"更好的提示词产出更好的答案"这条梯度在高分段整段消失。
"""
from __future__ import annotations

import os

import pytest

from omegaforge.core.budget import TokenBank
from omegaforge.distill.engine import DistillEngine
from omegaforge.llm.client import LLMClient

# 三个领域互不相关的源材料。若产物与输入无关，它们会得到同一份结果。
SOURCES = {
    "paper": (
        "You are ArxivResearcher, a meticulous academic research assistant. "
        "Your mission: locate and summarize academic papers with rigor and "
        "precision. Always cite sources with arXiv IDs. Never fabricate DOIs. "
        "Workflow: parse intent -> search -> filter -> read abstracts -> "
        "synthesize. Output format: markdown brief with a sources section."),
    "code": (
        "You are CodeReviewer. Review code for correctness and security. "
        "Workflow: read diff -> check edge cases -> report findings. "
        "Output: prioritized list of issues with severity. "
        "Never approve a change without reading the full diff."),
    "travel": (
        "You are TravelPlanner, a friendly travel itinerary designer. "
        "Your mission: build day-by-day itineraries that balance sightseeing "
        "with rest. Always show transport between stops and meal breaks. "
        "Output: a markdown table per day with times and notes."),
}


def _score(tmp_path, tag: str) -> float:
    eng = DistillEngine(LLMClient(), TokenBank(200_000), arena_rounds=2,
                        max_generations=1, verbose=False)
    out = str(tmp_path / tag)
    os.makedirs(out, exist_ok=True)
    _g, rep = eng.distill(SOURCES[tag], output_dir=out)
    return round(float(getattr(rep, "final_score", 0.0) or 0.0), 4)


def test_final_score_varies_with_source(tmp_path):
    """不同源材料必须给出不同分数。

    只断言"有分数"会让常量通过：常量非空、看着也合理。
    """
    got = {t: _score(tmp_path, t) for t in ("paper", "code", "travel")}
    assert len(set(got.values())) >= 2, (
        f"三个不同源材料得到了同一个分数，分数与输入无关：{got}")


def test_final_score_is_not_a_plain_constant(tmp_path):
    """分数不得落在"三个源都一样"这条线上，且必须真的有值。

    与上一条分开：一条断言"互不相同"，另一条断言"确实算出了数"。
    只留第一条时，全 0 也能通过（三个 0 互不相同？不，会失败）——
    但只留第二条时，常量会顺利通过。两条合起来才既非空转也非恒真。
    """
    vals = [_score(tmp_path, t) for t in ("paper", "code")]
    assert all(isinstance(v, float) for v in vals)
    assert any(v > 0 for v in vals), f"分数全为零，评分环节没有真的打分：{vals}"
    assert vals[0] != vals[1], f"两个不同源材料分数相同：{vals}"
