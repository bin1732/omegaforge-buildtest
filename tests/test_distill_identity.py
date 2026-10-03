# -*- coding: utf-8 -*-
"""蒸馏体的身份字段必须随源材料变化。

## 守的是什么

基因组页面把 `name` 作为条目标题展示。它若与源材料无关，
用户面对一列同名条目无法区分，界面上的"有名字"就成了空转。

这类失效不表现为报错：字段非空、看着也像个名字（"DistilledAgent"），
唯独不携带任何区分信息。只断言"名字非空"抓不到。

## 约束

一、不同自称必须产出不同名字。
二、名称提取不到时的兜底值必须保持可读拼写。
三、名称规范化不得压平驼峰。`.title()` 会把词内大写字母一并降级，
    "DistilledAgent" 因此显示为粘连的 "Distilledagent"。
"""
from __future__ import annotations

import os

import pytest

from omegaforge.core.budget import TokenBank
from omegaforge.distill.engine import DistillEngine
from omegaforge.llm.client import LLMClient, _norm_agent_name

# 四个领域互不相关的源材料，各自以最自然的方式自报身份
SOURCES = {
    "paper": ("You are ArxivResearcher, a meticulous academic research "
              "assistant. Workflow: parse intent -> search -> filter -> "
              "synthesize. Always cite arXiv IDs."),
    "code": ("You are CodeReviewer. Review code for correctness and "
             "security. Workflow: read diff -> check edge cases -> report "
             "findings. Never approve without reading the full diff."),
    "travel": ("You are TravelPlanner, a friendly itinerary designer. "
               "Workflow: gather preferences -> draft days -> balance rest "
               "-> add transport. Always show meal breaks."),
    "cooking": ("You are RecipeChef. Workflow: read ingredients -> propose "
                "steps -> give timings -> note substitutions. Always list "
                "allergens."),
}

# 描述性开场：不含大写标识符，属于"确实无法识别名称"的形态
ANONYMOUS = ("you are a meticulous research assistant. Workflow: search "
             "-> read -> write. Always cite sources.")


def _distill(tmp_path, tag: str, src: str):
    eng = DistillEngine(LLMClient(), TokenBank(200_000), arena_rounds=2,
                        max_generations=1, verbose=False)
    out = str(tmp_path / tag)
    os.makedirs(out, exist_ok=True)
    return eng.distill(src, output_dir=out)


def test_name_varies_with_source(tmp_path):
    """不同自报身份的源材料必须给出不同名字。

    只断言"名字非空"会让常量通过——常量同样非空、同样像个名字。
    """
    got = {}
    for tag, src in SOURCES.items():
        g, _ = _distill(tmp_path, tag, src)
        got[tag] = getattr(g, "name", None)
    assert all(got.values()), f"存在空名字：{got}"
    assert len(set(got.values())) == len(SOURCES), (
        f"不同源材料得到了相同的名字，名字与输入无关：{got}")


def test_fallback_name_stays_readable(tmp_path):
    """识别不出名称时，兜底值必须保持驼峰可读。"""
    g, _ = _distill(tmp_path, "anon", ANONYMOUS)
    name = getattr(g, "name", None)
    assert name, "兜底名称不得为空：界面条目会失去标题"
    assert name == "DistilledAgent", (
        f"兜底名称拼写被压平或改动：{name!r}")


def test_norm_agent_name_keeps_camel_case():
    """规范化不得压平驼峰。"""
    assert _norm_agent_name("DistilledAgent") == "DistilledAgent"
    assert _norm_agent_name("ArxivResearcher") == "ArxivResearcher"
    assert _norm_agent_name("sample_agent") == "Sample Agent"
    assert _norm_agent_name("source_alpha_bot") == "Source Alpha Bot"
    assert _norm_agent_name("") == "DistilledAgent"
    assert _norm_agent_name(None) == "DistilledAgent"
