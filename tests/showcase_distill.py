#!/usr/bin/env python3
"""DistillEngine 真实效果展示 — 单独验证蒸馏引擎。

用例 A：复杂 Python agent（数据分析师：4 工具 + 5 步工作流 + 质量栏 + 失败模式）
用例 B：markdown 人设（msitarzewski/agency-agents 151k★ 生态格式）
每个用例打印完整蒸馏产物：Spec → Genome → 编译 提示词 → 考卷 → Arena 对战 → 裁决 → token 台账
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.llm.client import LLMClient
from omegaforge.core.budget import TokenBank
from omegaforge.distill.engine import DistillEngine
from omegaforge.distill.loader import SourceAgentLoader

AGENT_A = '''class DataAnalystAgent:
  """Senior data analyst agent — profiles, cleans, analyzes, visualizes."""
  name = "InsightMiner"
  role_prompt = \'\'\'
You are a senior data analyst. You treat every dataset as guilty until
proven innocent: profile first, clean second, analyze third.
Persona: skeptical of dirty data, quantifies uncertainty, never presents
a number without its caveat, prefers tables before charts.
Workflow: profile dataframe -> clean anomalies -> run analysis -> make
chart -> write report with confidence notes.
Quality bars: every chart ships with a one-sentence takeaway; p-values
always reported with sample size n; outliers flagged but never silently
dropped.
Failure modes: chunking errors on wide tables, timezone mixups in time
series, silent dtype coercion corrupting categorical columns.
\'\'\'
  tools = ["load_csv(path)", "profile_dataframe(df)", "run_python(code)", "make_chart(data, kind)"]
'''

PERSONA_B = """# UX Research Agent

## Mission
Plan and run lightweight UX research: heuristic reviews, survey design,
usability findings synthesized into prioritized recommendations.

## System Prompt
You are a meticulous UX research agent. You ground every finding in
evidence, cite the heuristic or session it came from, and separate
observations from interpretations. You rank issues by severity x reach.

## Tools
- heuristic_check(design): Nielsen 10 heuristics inspection
- survey_generator(topic): builds neutral, bias-free survey questions
- session_notes_analyzer(notes): extracts findings from usability sessions

## Workflow
1. Plan the study and success criteria
2. Recruit representative scenarios
3. Run heuristic review and sessions
4. Synthesize findings with severity ratings
5. Deliver prioritized recommendations
"""


def showcase(tag: str, source_path: str, out_dir: str) -> dict:
  print(f"\n{'█' * 64}\n█ 用例 {tag}\n{'█' * 64}")
  llm = LLMClient()
  bank = TokenBank(400_000)
  engine = DistillEngine(llm, bank, verbose=True)

  print("\n── Step1 INGEST ──")
  sig = SourceAgentLoader().load(source_path)
  print(" ", sig.summary())
  print(" role_hints:", sig.role_hints)
  print(" tool_candidates:", sig.tool_candidates[:6])
  print(" workflow_cues:", sig.workflow_cues)

  print("\n── Step2 EXTRACT → SourceSpec ──")
  spec = engine.step_extract(sig)
  print(json.dumps(spec.__dict__, ensure_ascii=False, indent=2)[:1200])

  print("\n── Step3 COMPRESS → Genome ──")
  g = engine.step_compress(spec, lineage=f"distill-of:{sig.fingerprint}",
               fingerprint=sig.fingerprint)
  print(" name:", g.name)
  print(" mission:", g.mission_one_liner)
  print(" persona_genes:", g.persona_genes)
  print(" tool_genes:", g.tool_genes)
  print(" workflow_genes:", g.workflow_genes)
  print(" upgrade_genes:", g.upgrade_genes)
  print(" est_system_tokens:", g.est_system_tokens)

  print("\n── Step4 SYNTHESIZE → 编译产物 ──")
  g = engine.step_synthesize(g)
  print(" system_prompt (", len(g.system_prompt), "chars ):")
  print(" " + g.system_prompt[:600].replace("\n", "\n "))

  print("\n── Step5 GEN_EVAL → 考卷 ──")
  cases = engine.step_gen_eval(g)
  for c in cases:
    print(f" [{c['id']}] {c['input'][:80]}")
    print(f"    rubric: {c['rubric']}")

  print("\n── Step6 ARENA → 对战 ──")
  d_avg, b_avg, reasons, pairs = engine.run_arena(g, cases, 1)
  for p in pairs:
    print(f" case {p['case']}: baseline={p['score_baseline']}"
       f" distilled={p['score_distilled']} winner={p['winner']}"
       f" | {p['judge_reason']}")
  print(f" → 蒸馏体均分 {d_avg:.2f} vs 原版基线 {b_avg:.2f}")

  verdict = ("win 🏆 蒸馏体胜出" if d_avg > b_avg + 0.25
        else "tie 🤝 追平" if d_avg >= b_avg - 0.25
        else "loss ❌")
  print(f"\n── 裁决: {verdict} ──")
  print(" token 台账:")
  for ph, tk in bank.ledger_by_phase().items():
    print(f"  · {ph:<20} {tk:>6}")
  print(f"  合计 {bank.total_spent} / 预算 400000")

  os.makedirs(out_dir, exist_ok=True)
  g.save(os.path.join(out_dir, "genome.json"))
  with open(os.path.join(out_dir, "arena_pairs.json"), "w",
       encoding="utf-8") as f:
    json.dump(pairs, f, ensure_ascii=False, indent=2)
  return {"tag": tag, "verdict": verdict, "distilled": round(d_avg, 2),
      "baseline": round(b_avg, 2), "tokens": bank.total_spent,
      "prompt_chars": len(g.system_prompt)}


def main() -> None:
  tmp = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "output", "showcase")
  os.makedirs(tmp, exist_ok=True)
  pa = os.path.join(tmp, "source_data_analyst.py")
  open(pa, "w", encoding="utf-8").write(AGENT_A)
  pb = os.path.join(tmp, "source_ux_persona.md")
  open(pb, "w", encoding="utf-8").write(PERSONA_B)

  r1 = showcase("A · 复杂 Python 数据分析师 agent", pa,
         os.path.join(tmp, "case_a"))
  r2 = showcase("B · Markdown 人设（151k★ 生态格式）", pb,
         os.path.join(tmp, "case_b"))

  print(f"\n{'═' * 64}\n蒸馏引擎真实效果总结")
  for r in (r1, r2):
    print(f" 用例{r['tag']}: {r['verdict']} "
       f"蒸馏体 {r['distilled']} vs 基线 {r['baseline']} "
       f"token {r['tokens']} 编译提示词 {r['prompt_chars']} 字符")
  print(" 产物: output/showcase/case_a|case_b/{genome.json,arena_pairs.json}")
  with open(os.path.join(tmp, "showcase_summary.json"), "w",
       encoding="utf-8") as f:
    json.dump([r1, r2], f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
  main()
