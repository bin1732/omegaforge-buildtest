"""验证 Arena 对照组公平性修复。

针对已定位的两处"验证造假"：
 1. _simulate_baseline 用编造弱提示 → 改为源 agent 原始 提示词
 2. mock judge 写死 A=6.2/B=8.4 永远判 B 赢 → 改为基于内容特征打分
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.domain.baseline import build_baseline # noqa: E402
from omegaforge.distill.loader import SourceAgentLoader # noqa: E402
from omegaforge.llm.client import _score_answer, _split_arena_answers # noqa: E402
from omegaforge.core.budget import TokenBank # noqa: E402
from omegaforge.distill.engine import DistillEngine # noqa: E402
from omegaforge.llm.client import LLMClient # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


RICH_SOURCE = """You are ArxivResearcher, a meticulous academic research assistant.
Your mission: locate and summarize academic papers with rigor and precision.
Always cite sources with arXiv IDs. Never fabricate DOIs or page numbers.
You must verify every claim against at least one primary source before asserting it.
Tools available: arxiv_search(query), fetch_paper(id), extract_citations(text).
Workflow: parse intent -> search -> filter by relevance -> read abstracts -> synthesize.
Output format: markdown brief under 800 words with a sources section.
"""

# 一段真实代码：够长（>40 字符，不会触发 loader 的长度拦截），
# 但本身不是 提示词 —— 对照组应降级为 简化对照 并标注不可比。
THIN_SOURCE = """import os

def process_items(items):
  results = []
  for item in items:
    results.append(item.strip())
  return results

class Pipeline:
  def run(self, data):
    return process_items(data)
"""


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_arena_")
  try:
    loader = SourceAgentLoader()

    # ---------- 1. 对照组构建 ----------
    print("\n[1] 对照组来自源 agent 原始 prompt（不再是稻草人）")
    sig_rich = loader.load(RICH_SOURCE)
    b_rich = build_baseline(sig_rich)
    check("源含 prompt → kind=source_prompt",
       b_rich.kind == "source_prompt", b_rich.kind)
    check("标记为可比", b_rich.comparable is True)
    check("对照 prompt 取自源材料",
       b_rich.system_prompt.strip()[:40] in RICH_SOURCE,
       f"{b_rich.system_prompt[:40]}...")
    check("不再是编造的弱提示",
       "Answer the task thoroughly" not in b_rich.system_prompt,
       "已定位剔除")

    print("\n[2] 源无 prompt → 明确降级并标注不可比")
    sig_thin = loader.load(THIN_SOURCE)
    b_thin = build_baseline(sig_thin)
    check("kind=naive", b_thin.kind == "naive", b_thin.kind)
    check("comparable=False", b_thin.comparable is False)
    check("note 含不可比警示",
       "不可比" in b_thin.note or "不构成" in b_thin.note,
       b_thin.note[:40])

    print("\n[3] 用户显式指定优先")
    b_prov = build_baseline(sig_rich, provided_prompt="You are X. " * 30)
    check("kind=provided", b_prov.kind == "provided", b_prov.kind)
    check("provided 可比", b_prov.comparable is True)

    # ---------- 4. mock judge 基于内容 ----------
    print("\n[4] mock judge 分数由内容决定（不再写死）")
    good = ("## Summary\n"
        "- Point one with evidence: 2024 study shows 42.5% improvement\n"
        "- Second finding, see https://arxiv.org/abs/2401.1\n"
        "结论：有效。若不满足条件则降级处理。")
    bad = "yes"
    sg, sb = _score_answer(good), _score_answer(bad)
    ga = sum(sg.values()) / len(sg)
    ba = sum(sb.values()) / len(sb)
    check("优质答案得分高于劣质", ga > ba, f"{ga:.2f} vs {ba:.2f}")
    check("分数在 0-10 区间", all(0 <= v <= 10 for v in list(sg.values()) + list(sb.values())))
    check("不同内容产生不同分数", sg != sb)
    check("不再写死 8.4/6.2", abs(ga - 8.4) > 0.01 or abs(ba - 6.2) > 0.01,
       f"A={ba:.2f} B={ga:.2f}")

    # 胜负可 A 可 B（旧实现恒 B）
    print("\n[5] 胜负不预设")
    prompt = f"TASK: x\nRUBRIC: y\n\nANSWER A:\n{good}\n\nANSWER B:\n{bad}\n"
    a, b = _split_arena_answers(prompt)
    check("能切出 A/B 两段", a == good and b == bad,
       f"A={len(a)} B={len(b)} 字符")
    # A 好于 B 时，按同一评分逻辑 A 应胜
    check("A 更强时 A 得分更高",
       sum(_score_answer(a).values()) > sum(_score_answer(b).values()))

    # ---------- 6. 端到端 ----------
    print("\n[6] 端到端 distill 记录对照组可信度")
    llm = LLMClient()
    bank = TokenBank(500_000)
    eng = DistillEngine(llm, bank, arena_rounds=2,
              max_generations=1, verbose=False)
    g, report = eng.distill(RICH_SOURCE, output_dir=os.path.join(tmp, "out"))
    check("report 含 baseline_kind", bool(report.baseline_kind),
       report.baseline_kind)
    check("rich 源 → 可比对照", report.baseline_comparable is True,
       f"kind={report.baseline_kind}")
    # 契约已加强：结论成立还要求有样本、且评分未被污染
    check("claim_valid 与三个条件一致",
       report.claim_valid == (report.verdict == "win"
                   and report.baseline_comparable
                   and report.arena_cases > 0
                   and report.contaminated_cases == 0),
       f"verdict={report.verdict} cases={report.arena_cases} "
       f"contam={report.contaminated_cases} "
       f"claim_valid={report.claim_valid}")

    # 薄源 → 不可比，即便赢也不该宣称更强
    eng2 = DistillEngine(llm, TokenBank(500_000), arena_rounds=1,
               max_generations=1, verbose=False)
    try:
      g2, r2 = eng2.distill(THIN_SOURCE, output_dir=os.path.join(tmp, "out2"))
      check("薄源 → comparable=False",
         r2.baseline_comparable is False, r2.baseline_kind)
      check("薄源下 claim_valid 必为 False",
         r2.claim_valid is False,
         f"verdict={r2.verdict} comparable={r2.baseline_comparable}")
    except Exception as e:            # noqa: BLE001
      check("薄源 distill 不崩溃", False, f"{type(e).__name__}: {e}")

    # 产物落盘
    check("产出 genome.json",
       os.path.exists(os.path.join(tmp, "out", "genome.json")))
    check("产出 report.json",
       os.path.exists(os.path.join(tmp, "out", "report.json")))

  finally:
    shutil.rmtree(tmp, ignore_errors=True)

  print(f"\n{'='*46}")
  print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
  if FAIL:
    for f in FAIL:
      print(" FAILED:", f)
  return 1 if FAIL else 0




# ── pytest 入口 ──────────────────────────────────────────────────────
# 本文件原本只支持 `python3 tests/xxx.py` 独立运行，被 pytest 收集时
# 收集到 0 个用例（没有 test_ 函数），因此在 CI 里从未真正执行过——
# 守卫写了但不跑，等于没写。加这一层让它在两种入口下都跑真用例。
def test_arena_fairness() -> None:
  assert main() == 0

if __name__ == "__main__":
  raise SystemExit(main())
