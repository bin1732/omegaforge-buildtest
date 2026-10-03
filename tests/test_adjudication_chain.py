"""裁判链（adjudication chain）守卫：谁出的题、谁答的、谁判的。

背景（可复现，不是推断）
--------------------------
1. **默认配置下裁判即被测方**：OMEGAFORGE_MODEL_FAST 与
  OMEGAFORGE_MODEL_JUDGE 的缺省值相同，作答与裁判本就是同一个模型。
  LLM judge 的自偏好（倾向给自己/同族模型的输出打高分）是**系统性**
  偏差——它作用于每一个用例，不会像位置偏差那样靠多次平均抵消。
  而产物里原先**没有任何字段**记录谁判的，结论成立标记照样为真。

2. **产物无从复核**：验证产物字段共 14 个，含 model/judge 字样的只有
  `judge_reasons`（那是评审意见文本，不是模型身份）。用户拿到"更强"
  的结论，却看不到它是自证的还是被测出来的。

3. **fail-closed**：模型信息缺失时按"未排除自证"处理。空值只可能来自
  "忘了填"，而"忘了填"不该被当成"已排除"。

本文件的守卫全部做过回退校验（撤掉修复必须变红），不是形式化。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.core.budget import TokenBank        # noqa: E402
from omegaforge.distill.engine import (          # noqa: E402
  DistillEngine, DistillReport)
from omegaforge.llm.client import LLMResponse       # noqa: E402

DIMS = ("task_completion", "correctness", "efficiency", "robustness", "polish")

# 足够完整，能让 loader 判为可比对照组（太薄会落到 简化对照，
# 那是既有闸门，与改动无关，会干扰这里的判定）
SOURCE = """You are a senior research analyst agent.

ROLE: 负责把原始材料整理成结构化结论。
WORKFLOW: 1) 读取材料 2) 抽取要点 3) 交叉验证 4) 输出结论。
TOOLS: fs_read, web_search, kb_search.
QUALITY: 结论必须有出处；不确定时必须说明不确定，不得编造。

You must always cite sources. Structure every output with headings.
Never fabricate. If evidence is insufficient, say so explicitly and
stop rather than guessing. Prefer primary sources over secondary ones.
Cross-check every claim against at least two independent sources.
"""

SPEC = {"name": "T", "role": "r", "mission": "m", "persona": "p",
    "tools": [], "workflow": [], "io": {}, "failure_modes": [],
    "quality_bars": []}
GENOME_P = {"name": "T", "mission_one_liner": "m", "persona_genes": ["a"],
      "tool_genes": ["b"], "workflow_genes": ["c"], "upgrade_genes": [],
      "est_system_tokens": 100}
SYNTH = {"system_prompt": "distilled prompt", "tools": [], "workflow": []}


class StageMock:
  """按阶段路由的替身。judge 模型可配，用来构造自证/非自证两种场景。

  两条验证踩到的坑，都写进注释免得后来者重犯：
   - 不能用 "distillation core" 判裁判：它是 _chat_json 的公共
    system 前缀，出题/压缩/合成全走这里，按它路由必然串台。
   - 各阶段的实际 prompt 在 **user** 里，按 system 路由同样串台。
   - 作答阶段两边若返回同一段文本，两个顺序的裁判输入完全相同，
    去偏无从区分，得分必然扯平（验证 6.0 平局，掩盖了真实差距）。
  """

  model_main = model_fast = "gpt-4o-mini"

  def __init__(self, judge_model="gpt-4o-mini"):
    self.model_judge = judge_model
    self._canon = None

  def chat(self, system, user, model=None, max_tokens=None, json_mode=False):
    if "ANSWER A:" in user:
      a_txt, b_txt = user.split("ANSWER A:", 1)[1].split("ANSWER B:", 1)
      a_txt, b_txt = a_txt.strip(), b_txt.strip()
      if self._canon is None:
        self._canon = (a_txt, b_txt)
      _, dist_txt = self._canon
      # 蒸馏体恒胜，让 verdict=win，从而单独检验自证闸门
      s = ({"A": {k: 9 for k in DIMS}, "B": {k: 3 for k in DIMS}}
         if a_txt == dist_txt
         else {"A": {k: 3 for k in DIMS}, "B": {k: 9 for k in DIMS}})
      w = "A" if s["A"]["polish"] > s["B"]["polish"] else "B"
      return LLMResponse(text=json.dumps(
        {"scores": s, "reason": "r", "winner": w}),
        prompt_tokens=1, completion_tokens=1, model=self.model_judge)
    if "AgentArchaeologist" in user:
      return LLMResponse(text=json.dumps(SPEC), prompt_tokens=1,
                completion_tokens=1, model="gpt-4o-mini")
    if "Compress this agent SPEC" in user:
      return LLMResponse(text=json.dumps(GENOME_P), prompt_tokens=1,
                completion_tokens=1, model="gpt-4o-mini")
    if "Compile this Genome" in user:
      return LLMResponse(text=json.dumps(SYNTH), prompt_tokens=1,
                completion_tokens=1, model="gpt-4o-mini")
    if "Design a compact exam" in user:
      return LLMResponse(text=json.dumps(
        {"cases": [{"id": "c1", "input": "task one",
              "rubric": ["r"]}]}),
        prompt_tokens=1, completion_tokens=1, model="gpt-4o-mini")
    if system == "distilled prompt":
      return LLMResponse(text="DISTILLED-ANSWER", prompt_tokens=1,
                completion_tokens=1, model="gpt-4o-mini")
    return LLMResponse(text="BASELINE-ANSWER", prompt_tokens=1,
              completion_tokens=1, model="gpt-4o-mini")


def _distill(judge_model: str, task: str = "") -> DistillReport:
  llm = StageMock(judge_model=judge_model)
  eng = DistillEngine(llm=llm, bank=TokenBank(budget_tokens=10**7),
            arena_rounds=1, max_generations=1, verbose=False,
            task=task)
  out = tempfile.mkdtemp()
  try:
    _, rep = eng.distill(SOURCE, output_dir=out)
    return rep
  finally:
    shutil.rmtree(out, ignore_errors=True)


# ------------------------------------------------------------ 1 引擎必须填链

def test_engine_populates_adjudication_chain():
  """走完整蒸馏路径：产物里必须有裁判链。

  只测产物对象字段默认值是**测不出接通**的——引擎没填也会全绿。
  回退校验：注掉 distill() 里给 answer_model/judge_model 赋值的两行，
  本项失败。
  """
  rep = _distill("claude-haiku")
  assert rep.answer_model, "作答模型未写入产物"
  assert rep.judge_model, "裁判模型未写入产物"
  assert rep.question_source in ("llm", "builtin"), "出题来源未写入产物"


def test_question_source_reflects_auto_generation():
  """自动出题成功时记为 llm，失败兜底时记为 builtin。"""
  assert _distill("claude-haiku").question_source == "llm"


# ------------------------------------------------------------ 2 自证闸门

def test_self_certified_blocks_claim_end_to_end():
  """默认配置（裁判==作答）：即便赢了，「更强」也不成立。

  这是本次改动最核心的一条。验证：蒸馏体 9.0 / 对照 3.0、verdict=win，
  但裁判与作答同源，结论属自证，结论是否成立 必须为假。
  回退校验：去掉结论成立标记里的裁判自证排除条件，
  本项失败。
  """
  rep = _distill("gpt-4o-mini")     # 与 model_fast 相同
  assert rep.self_certified is True
  assert rep.verdict == "win", "替身必须构造出获胜场景，否则测不到闸门"
  assert rep.claim_valid is False, "裁判即被测方时不得给出「更强」结论"


def test_distinct_judge_allows_claim():
  """裁判独立 **且** 评分标准非被测方自定 → 结论可采信（未被过度设闸）。

  这条是防止"处理过头"。注意它现在必须自带考题（task）：换裁判只切断
  "谁来判"，而"考什么、按什么标准判"仍由被测方说了算——验证默认配置
  下裁判指令 100% 含被测方自定的 rubric。因此可信路径是"换裁判 +
  自己出题"，两者缺一不可。若本项在只换裁判时也通过，说明出题侧
  闸门没生效。
  """
  rep = _distill("claude-haiku", task="我自己的考题：请把这份材料整理成结论")
  assert rep.self_certified is False, "裁判已换成第三方，不该判为自证"
  assert rep.exam_self_authored is False, \
    "考题与标准均由用户提供，不该判为被测方自定"
  assert rep.claim_valid is True, \
    "裁判独立且标准人工给定时结论应可采信（闸门处理过头会在这里红）"


def test_exam_self_authored_blocks_claim_even_with_distinct_judge():
  """换第三方裁判也绕不开出题侧闭环——这是本次改动的核心发现。

  验证：judge=claude-haiku、answer=gpt-4o-mini（裁判自证为否）
  时，两条裁判指令里**全都**含被测方自定的 rubric。裁判再独立，也是
  按被测方定的尺子打分。不设这条闸门，用户换个裁判就能拿到看似可信、
  实则仍自说自话的结论。
  """
  rep = _distill("claude-haiku")     # 不给 task → 自动出题
  assert rep.self_certified is False, "裁判确为第三方，本项检验的是出题侧"
  assert rep.exam_self_authored is True, \
    "考题/标准由被测方自定却未被识别"
  assert rep.claim_valid is False, \
    "出题侧自证时 claim_valid 必须为假，换裁判也不能放行"


def test_engine_records_exam_provenance():
  """出题模型与标准来源必须落进产物，否则用户无从复核这一层。

  回退校验：注掉 distill() 里给 question_model/rubric_source 赋值的
  两行，本项失败。
  """
  rep = _distill("claude-haiku")
  assert rep.rubric_source == "llm", "自动出题时标准来源应记为 llm"
  assert rep.question_model, "出题模型未写入产物"
  rep2 = _distill("claude-haiku", task="我自己的考题")
  assert rep2.rubric_source == "user", \
    "用户出题且只评这道题时，标准来源应记为 user"
  assert rep2.question_model == "", \
    "模型出的题没被评到，就不该让结论为它背锅"


def test_self_certified_trust_note_is_actionable():
  """自证时给的理由必须告诉用户**怎么修**，而不只是说"不可信"。"""
  rep = _distill("gpt-4o-mini")
  assert "OMEGAFORGE_MODEL_JUDGE" in rep.trust_note, \
    "trust_note 必须给出可执行的修复路径（换裁判模型）"
