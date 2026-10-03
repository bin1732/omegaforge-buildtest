"""校验：对照提示词门槛修复的四个校验点。

判定不看退出码，看**哪几条用例失败了**。只凭 rc 非零会把两件事误判成抓到：
收集阶段就报错（用例一条没跑），以及失败的是别的用例——两者都满足"撤掉修复
后出现失败"，于是校验点恒真。

每个校验点因此都要点名：撤掉该处改动后，具体哪条用例必须失败。

另一条同样重要：注入点随代码漂移而失效时，必须算失败。锚点失效若只是打印
一句话然后跳过，收尾仍返回 0，于是零个校验点执行过也报通过——统一入口按
退出码判定，会把"没验过"读成"验过且通过"。

| 校验点 | 注入 | 预期失败的用例 |
|------|------|------|
| A | 源材料分支退回纯字符门槛 | 源材料分支中文提示通过 |
| B | 用户提供分支退回纯字符门槛 | 中文提示通过信息量门槛 |
| C | 撤掉"有候选但不够格"与"真的没有候选"的文案区分 | 有候选时不得谎称没有 |
| D | 信息量门槛降到 1（防修过头） | 短提示仍被拒绝 |

运行：  python3 probes/rev_prompt_threshold.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "omegaforge" / "domain" / "baseline.py"
TEST = "tests/test_baseline_prompt_threshold.py"
ORIG = TARGET.read_text(encoding="utf-8")

P = TEST + "::"

ANCHORS = [
    ("A 源材料分支退回纯字符门槛",
     "        if _enough(best):",
     "        if len(best) >= MIN_PROMPT_CHARS:",
     [P + "test_source_branch_chinese_prompt_passes",
      P + "test_both_branches_share_one_threshold"]),
    ("B 用户提供分支退回纯字符门槛",
     "        if not _enough(given):",
     "        if len(given) < MIN_PROMPT_CHARS:",
     [P + "test_chinese_prompt_passes_weight_threshold",
      P + "test_chinese_and_english_are_not_judged_by_different_rulers",
      P + "test_weight_boundary",
      P + "test_both_branches_share_one_threshold"]),
    ("C 撤掉是否有候选的文案区分",
     "    if cands:\n        note = (f\"源材料中提取到的系统提示词信息量不足\"",
     "    if False:\n        note = (f\"源材料中提取到的系统提示词信息量不足\"",
     [P + "test_source_branch_note_does_not_claim_absent_when_present"]),
    ("D 信息量门槛降到 1（防修过头）",
     "MIN_PROMPT_WEIGHT = MIN_PROMPT_CHARS / 5",
     "MIN_PROMPT_WEIGHT = 1.0",
     [P + "test_short_prompts_still_rejected",
      P + "test_weight_boundary",
      P + "test_too_short_note_reports_both_measures",
      P + "test_source_branch_note_does_not_claim_absent_when_present",
      P + "test_both_branches_share_one_threshold"]),
]


def main() -> int:
    bad = []
    try:
        TARGET.write_text(ORIG, encoding="utf-8")
        rc, _failed, tail = pytest_run(TEST)
        print(f"[基线] rc={rc}  {tail}")
        if rc != 0:
            print("基线未通过，反向验证无意义")
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"[基线] 无法运行：{e}")
        return 1

    for name, old, new, expect in ANCHORS:
        if old not in ORIG:
            # 锚点失效不等于通过：一个没跑的校验点不能被记成抓到。
            print(f"[{name}] 注入点不存在 —— 锚点失效，须重定位")
            bad.append(name)
            continue
        patched = ORIG.replace(old, new, 1)
        try:
            ast.parse(patched)
        except SyntaxError as e:
            print(f"[{name}] 注入后语法损坏（不可作为证据）: {e}")
            bad.append(name)
            continue
        try:
            TARGET.write_text(patched, encoding="utf-8")
            rc, failed, tail = pytest_run(TEST)
        finally:
            TARGET.write_text(ORIG, encoding="utf-8")
        caught, why = verdict(rc, failed, expect)
        print(f"[{name}] rc={rc}  {tail}  → {'抓到' if caught else '未抓到'}（{why}）")
        if not caught:
            bad.append(name)

    rc, _failed, tail = pytest_run(TEST)
    print(f"[还原] rc={rc}  {tail}")
    if rc != 0:
        bad.append("还原")
    if bad:
        print(f"\n结论：{len(bad)}/{len(ANCHORS) + 1} 项未达标：{bad}")
        return 1
    print(f"\n结论：{len(ANCHORS)} 个校验点全部抓到，还原后全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
