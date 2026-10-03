# -*- coding: utf-8 -*-
"""离线答题必须是"参考变换"：输出由输入决定，不是固定文本。

## 守的是什么

离线分支自述为 REFERENCE TRANSFORM —— 输入决定输出，因此整条流水线
可以离线跑通且不同来源得到不同结果。这一契约一旦在某个分支上破掉，
该分支就退化成"每次都答同一句话"，而上层看不出来：

- 出题写死 → 不论蒸馏的是什么，考的都是同一套无关题目，评测集指纹恒定；
- 冠词剥离误伤 → 名字首字被吃掉，且它随后进到基因组、评测题与对照报告。

两类失效都不报错：字段非空、结构完整、指纹也有值，唯独与输入无关。
只断言"非空""有值"抓不到，必须断言"随输入变化"。

## 约束

一、出题必须随使命变化。
二、名字首字不得被当成冠词削掉。
三、使命只剩名字时不得直接拿来当使命（没有动作就没有可判分的东西）。
"""
from __future__ import annotations

import json

import pytest

from omegaforge.distill.engine import GEN_EVAL_PROMPT
from omegaforge.llm.client import LLMClient

_CLIENT = LLMClient()


def _mock(user: str, system: str = "You are OmegaForge distillation core.") -> str:
    return _CLIENT._mock(system, user, "mock-model").text


def _eval_cases(mission: str, role: str = "specialist") -> list:
    prompt = (GEN_EVAL_PROMPT
              .replace("<<MISSION>>", mission)
              .replace("<<ROLE>>", role))
    return json.loads(_mock(prompt))["cases"]


def _extracted(mission_sentence: str, name_hint: str = "") -> dict:
    payload = {"name_hint": name_hint,
               "prompt_candidates": [mission_sentence]}
    return json.loads(_mock("SIGNALS:\n" + json.dumps(payload,
                                                      ensure_ascii=False)))


def test_eval_cases_vary_with_mission():
    a = _eval_cases("search academic papers and write literature briefs")
    b = _eval_cases("turn leftover ingredients into a workable recipe")
    assert a and b
    assert [c["input"] for c in a] != [c["input"] for c in b], (
        "出题与使命无关：不同使命得到同一套题，评测集指纹随之恒定")


def test_eval_cases_carry_the_mission():
    cases = _eval_cases("plan multi-day itineraries within a budget")
    joined = " ".join(c["input"] for c in cases)
    assert "plan multi-day itineraries" in joined, (
        f"考题里没有使命内容，考的是与被测能力无关的东西：{joined[:120]}")


@pytest.mark.parametrize("name", ["ArxivScholar", "AnAnalyst", "HomeChef"])
def test_leading_letter_is_not_eaten_as_article(name):
    """`You are <Name>` 里的首字不得被当成冠词削掉。

    `(an?|the)?\\s*` 会把 ArxivScholar 的 A、AnAnalyst 的 An 当成冠词，
    使命变成 rxivScholar / Analyst——名字被截断而无人察觉。
    """
    out = _extracted(f"You are {name}. Do the work carefully.",
                     name_hint=name)
    assert out["name"] == name
    assert name in (out["mission"] or ""), (
        f"使命里名字被削掉了开头：{out['mission']!r}")


def test_mission_falls_back_when_first_sentence_is_only_a_name():
    out = _extracted("You are HomeChef. Turn the fridge into a recipe.")
    mission = out["mission"] or ""
    assert " " in mission, (
        f"使命只有一个词，评测题会变成没有动作的空壳：{mission!r}")
