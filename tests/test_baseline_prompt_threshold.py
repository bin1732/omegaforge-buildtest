"""对照提示词的"够不够格"判定：中英文必须同一把尺子。

覆盖两处失效：

1. 只按字符数卡门槛（120 字符），中文被系统性低估约 5 倍——
  42 字的完整中文提示词信息量高于 127 字符的英文，却被判"不足"，
  对照组降级为不可比，结论不成立。而 core/validate 提供的信息量
  权重函数当时全仓无调用者：修复写了，没接通。

2. 源材料里**有** prompt 候选只是不够格时，兜底文案写死"未包含可提取
  的 系统提示词"——材料里明明有，却说没有。给错原因会让人反复去
  翻材料，而翻多少遍结果都一样。

两处门槛必须共用同一个判定函数，分开写必然漏掉其中一处；因此守卫
对两条来源路径（用户提供 / 源材料提取）各设一份。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.core.validate import text_weight # noqa: E402
from omegaforge.domain.baseline import ( # noqa: E402
  MIN_PROMPT_CHARS, MIN_PROMPT_WEIGHT, build_baseline,
)

ZH = "你是一位资深代码审查专家，请检查代码中的并发与资源泄漏问题，并给出可执行的修改建议。"
EN = ("You are a senior code review expert. Please check the code for "
   "concurrency and resource leak issues, and give actionable fixes.")


class _Sig:
  """SourceSignals 的最小替身，只提供对照组构建用到的字段。"""

  def __init__(self, candidates=None, name_hint="助手", fingerprint="fp-1"):
    self.name_hint = name_hint
    self.prompt_candidates = list(candidates or [])
    self.fingerprint = fingerprint


# ───────────────────────── 用户显式提供 ─────────────────────────

def test_chinese_prompt_passes_weight_threshold():
  """42 字中文信息量高于门槛，必须放行——这是本次改动修的核心缺陷。"""
  assert text_weight(ZH) >= MIN_PROMPT_WEIGHT
  b = build_baseline(_Sig(), provided_prompt=ZH)
  assert b.kind == "provided"
  assert b.comparable is True


def test_english_prompt_still_passes():
  b = build_baseline(_Sig(), provided_prompt=EN)
  assert b.kind == "provided"
  assert b.comparable is True


def test_chinese_and_english_are_not_judged_by_different_rulers():
  """同一信息量档位的中英文必须同判，中文不得被系统性拒绝。"""
  assert build_baseline(_Sig(), provided_prompt=ZH).kind == \
      build_baseline(_Sig(), provided_prompt=EN).kind


@pytest.mark.parametrize("short", ["hi", "你好", "ok", "重要", "你是一位助手"])
def test_short_prompts_still_rejected(short):
  """防处理过头：放宽门槛不能把 2 字符的洗白口子重新打开。"""
  b = build_baseline(_Sig(), provided_prompt=short)
  assert b.kind == "naive"
  assert b.comparable is False


def test_weight_boundary():
  """门槛换算：120 字符西文 ≈ 24 个词，故 24 汉字通过、23 汉字不通过。"""
  assert MIN_PROMPT_WEIGHT == MIN_PROMPT_CHARS / 5
  assert build_baseline(_Sig(), provided_prompt="字" * 24).kind == "provided"
  assert build_baseline(_Sig(), provided_prompt="字" * 23).kind == "naive"


def test_too_short_note_reports_both_measures():
  """不够格时要把字符数与信息量两个数字都给出，只说字符数会误导中文用户。"""
  b = build_baseline(_Sig(), provided_prompt="你好")
  assert "字符" in b.note and "信息量" in b.note


# ───────────────────────── 源材料提取 ─────────────────────────

def test_source_branch_chinese_prompt_passes():
  """源材料提取是用户不填时的默认路径，中文源材料同样不能误杀。"""
  b = build_baseline(_Sig(candidates=[ZH]))
  assert b.kind == "source_prompt"
  assert b.comparable is True


def test_source_branch_note_does_not_claim_absent_when_present():
  """材料里明明有候选，只是不够格，不能谎报"未包含可提取的 提示词"。"""
  b = build_baseline(_Sig(candidates=["你好"]))
  assert b.kind == "naive"
  assert "未包含" not in b.note


def test_source_branch_note_claims_absent_only_when_truly_empty():
  b = build_baseline(_Sig(candidates=[]))
  assert b.kind == "naive"
  assert "未包含" in b.note


def test_both_branches_share_one_threshold():
  """两条来源路径必须同一把尺子：同一段文本在两条路径上可比性一致。

  kind 本就不同（provided / source_prompt 记录的是来源，不是判定），
  可比性才是那把尺子，因此断言落在 comparable 上。
  """
  via_provided = build_baseline(_Sig(), provided_prompt=ZH)
  via_source = build_baseline(_Sig(candidates=[ZH]))
  assert via_provided.comparable == via_source.comparable is True

  via_provided_short = build_baseline(_Sig(), provided_prompt="你好")
  via_source_short = build_baseline(_Sig(candidates=["你好"]))
  assert via_provided_short.comparable == via_source_short.comparable is False
