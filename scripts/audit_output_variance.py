# -*- coding: utf-8 -*-
"""产物字段是否随输入变化。

## 守的是什么

蒸馏产物里与输入无关、却显示为具体数值的字段，属于"看起来有值、
实际不携带信息"。用户在报告里看到"蒸馏体 8.57 分"，会当成蒸馏质量
的度量；在基因组页看到一列同名条目，会以为那是它们的名字。

这类字段不报错、不为空、看着也合理，任何"字段存在 / 非空"的断言
都抓不到。只有拿多个互不相同的源材料各跑一遍、逐个字段比对才现形。

## 判定

每个恒定字段必须落在两张表之一：

  * MUST_VARY —— 必须随输入变化，恒定即判失败；
  * ALLOW_CONST —— 允许恒定，但必须写明理由，未登记则报"未表态"。

未登记的恒定字段一律报出。不表态不能默认放行，否则本脚本会退化成
每次都通过、什么也没查。

## 用法

    python scripts/audit_output_variance.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.core.budget import TokenBank          # noqa: E402
from omegaforge.distill.engine import DistillEngine   # noqa: E402
from omegaforge.llm.client import LLMClient           # noqa: E402

# 领域互不相关的源材料。若产物与输入无关，它们会得到同一份结果。
SOURCES = {
    "paper": ("You are ArxivResearcher, a meticulous academic research "
              "assistant. Workflow: parse intent -> search -> filter -> "
              "read abstracts -> synthesize. Always cite arXiv IDs."),
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

# 必须随输入变化：这些字段直接作为产物身份或质量结论呈现给用户
MUST_VARY = {
    "genome.name": "基因组页的条目标题；恒定则用户无法区分不同蒸馏体",
    "genome.system_prompt": "蒸馏体的主体内容",
    "genome.source_fingerprint": "来源指纹，用于区分产物归属",
    "genome.workflow_genes": "工作流基因，承载源材料的工作方式",
    "genome.persona_genes": "人格基因，承载源材料的表达风格",
    "report.final_score": "报告里最显眼的分数，被当作蒸馏质量的度量",
    "report.judge_reasons": "逐条评分理由，分数背后依据",
    "report.source_signals": "源材料摘要，写进报告与运行记录",
}

# 允许恒定：结构性 / 计数类字段，取值由配置决定而非源材料
ALLOW_CONST = {
    "genome.arena_generation": "由 max_generations 决定，非源材料属性",
    "genome.baseline_tokens_per_task": "由 est_system_tokens 推算的预算基线",
    "genome.schema_version": "结构版本号",
    "genome.tools": "源材料未声明工具时无工具可提取",
    "genome.tool_genes": "同上",
    "genome.upgrade_genes": "质量约束条目，由配置与质量条决定",
    "report.ablation": "未开 --ablate 时无消融数据",
    "report.answer_model": "作答模型名，由配置决定",
    "report.arena_cases": "竞技场用例数，由 arena_rounds 决定",
    "report.baseline_comparable": "无基线时不可比",
    # 待办：基线侧同样应随源材料变化，否则"蒸馏变强了多少"缺少依据。
    # 当前 baseline_comparable 恒为否，报告不以此下结论，故记为已知项。
    "report.baseline_score": "基线侧为固定参考；当前不可比，不据此下结论",
    "report.baseline_kind": "基线类型，由配置决定",
    "report.best_generation": "由 max_generations 决定",
    "report.claim_valid": "裁判与作答同模型时恒为不可采信",
    "report.contaminated_cases": "注入污染计数，正常为零",
    "report.debiased_cases": "去偏用例数，由 arena_rounds 决定",
    "report.eval_set_cases": "评测集条数，由配置决定",
    "report.eval_set_fingerprint": "评测集指纹，由配置决定",
    "report.eval_set_source": "评测集来源，由配置决定",
    "report.evolution_notes": "单代蒸馏无进化记录",
    "report.generation": "由 max_generations 决定",
    "report.generations_run": "同上",
    "report.question_model": "出题模型名，由配置决定",
    "report.judge_model": "裁判模型名，由配置决定",
    "report.question_source": "题目来源，由配置决定",
    "report.rolled_back": "无回滚时恒为否",
    "report.rubric_source": "评分标准来源，由配置决定",
    "report.self_certified": "自认证标记，默认关闭",
    "report.trust_note": "信任说明文案，取值由裁判配置决定",
}

GENOME_KEYS = ["arena_best_score", "arena_generation", "arena_history",
               "baseline_tokens_per_task", "diff_summary",
               "est_system_tokens", "lineage", "mission_one_liner", "name",
               "persona_genes", "source_fingerprint", "system_prompt",
               "tool_genes", "tools", "upgrade_genes", "workflow",
               "workflow_genes"]

REPORT_KEYS = ["ablation", "answer_model", "arena_cases",
               "baseline_comparable", "baseline_kind", "baseline_score",
               "best_generation", "claim_valid", "contaminated_cases",
               "debiased_cases", "eval_set_cases", "eval_set_fingerprint",
               "eval_set_source", "evolution_notes", "final_score",
               "generation", "generations_run", "judge_model",
               "judge_reasons", "question_model", "question_source",
               "rolled_back", "rubric_source", "self_certified",
               "source_signals", "trust_note", "verdict"]


def _fingerprint(value) -> str:
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          default=str)
    except Exception:
        blob = str(value)
    return hashlib.md5(blob.encode()).hexdigest()[:8]


def collect(workdir: str) -> dict:
    """跑完全部源材料，返回 {字段名: {源: 指纹}}。"""
    table: dict = {}
    for tag, src in SOURCES.items():
        eng = DistillEngine(LLMClient(), TokenBank(200_000), arena_rounds=2,
                            max_generations=1, verbose=False)
        out = os.path.join(workdir, tag)
        os.makedirs(out, exist_ok=True)
        genome, report = eng.distill(src, output_dir=out)
        for prefix, obj, keys in (("genome", genome, GENOME_KEYS),
                                  ("report", report, REPORT_KEYS)):
            for k in keys:
                if not hasattr(obj, k):
                    continue
                table.setdefault(f"{prefix}.{k}", {})[tag] = _fingerprint(
                    getattr(obj, k))
    return table


def audit(table: dict) -> tuple[list, list, list]:
    """返回 (必须变化却恒定, 未登记的恒定字段, 恒定字段一览)。"""
    const_fields = []
    for field, per_source in sorted(table.items()):
        if len(per_source) >= 2 and len(set(per_source.values())) == 1:
            const_fields.append((field, list(per_source.values())[0]))

    must_vary_failed = [(f, MUST_VARY[f]) for f, _ in const_fields
                        if f in MUST_VARY]
    undeclared = [(f, v) for f, v in const_fields
                  if f not in MUST_VARY and f not in ALLOW_CONST]
    return must_vary_failed, undeclared, const_fields


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        table = collect(workdir)

    missing = [f for f in MUST_VARY if f not in table]
    if missing:
        print(f"产物里缺少应检查的字段：{missing}")
        print("字段被改名或删除后本脚本会静默少查，故视为失败")
        return 1

    failed, undeclared, const_fields = audit(table)

    print(f"源材料 {len(SOURCES)} 份，比对字段 {len(table)} 个，"
          f"其中恒定 {len(const_fields)} 个")

    if failed:
        print("\n=== 必须随输入变化，实际恒定 ===")
        for field, why in failed:
            print(f"  {field}   {why}")

    if undeclared:
        print("\n=== 恒定但未登记（不表态不默认放行）===")
        for field, fp in undeclared:
            print(f"  {field}   指纹={fp}")

    if failed or undeclared:
        print(f"\n结果: 失败（必变 {len(failed)} / 未登记 {len(undeclared)}）")
        return 1

    print("\n结果: 通过（恒定字段均已登记理由，必变字段均随输入变化）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
