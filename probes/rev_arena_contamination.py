#!/usr/bin/env python3
"""竞技场污染与胜负映射守卫的判定校验。

## 为什么补这个脚本

守卫是否成立，只能由"撤掉改动后用例是否变红"来证明。写在用例文件说明里
的"已做过回退校验"无法被复查，也挡不住代码漂移后守卫静默失效——全量回归
通过看不出区别，因为恒真的守卫同样是绿的。本脚本把这类声称换成可执行、
可重跑的证据。

每个校验点都点名到具体用例，判定采用严格模式：预期的那几条必须变红，且
不能出现预期之外的失败项——后者意味着"整片都红"，那时任何点名都会被满足。

## 各校验点守住的形态

 * A 撤掉操纵句式检测：污染样本与正常样本无法区分，产物里也不会有任何
   标记——用户拿到被操纵的评分却毫无线索。
 * B 胜负映射两个分支一起写反：不报错，产物里 winner 与得分恒相反。分歧
   检测会两边同错、互相抵消，只有拿 winner 和分数对照才暴露，故不能靠
   A 的断言覆盖。
 * D 两侧全败仍按 0:0 计入均值：把"没评出来"伪装成"双方都得零分"。
 * E 用例数每代重置：分子跨代累加而分母只反映最后一代，比例在数学上不
   可能成立。
 * F 去偏判定恒真：单顺序结果被当成去偏结果交出去。
 * G judge 提示词不降格为数据：被测方答案以"指令"而非"数据"的身份进上下文。
 * H 分隔标记带位号：同一内容在两个顺序里得到不同包裹，又添一层位置依赖。
 * J 操纵检测恒命中（反向）：正常答案被标成操纵，结论是否成立永远为假。
 * K 分歧不报：位置偏好被抵消后胜负仍指向一方，去偏形同失效。
 * L 零用例分支被摘：结论不成立却不给任何原因。
 * M 出题来源不落产物：用户无从复核题目是谁出的。

用法：

    python3 probes/rev_arena_contamination.py            # 全部校验点
    python3 probes/rev_arena_contamination.py --only A,B # 只跑指定前缀
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_arena_contamination.py"
C = TESTS + "::"

PROV = ROOT / "omegaforge" / "tools" / "provenance.py"
ENGINE = ROOT / "omegaforge" / "distill" / "engine.py"

ANCHORS = [
    ("A 撤掉操纵句式检测", PROV,
     '    norm = normalize(text)\n'
     '    hits = []\n'
     '    for tag, rx in _JUDGE_COMPILED:\n'
     '        if rx.search(norm) and tag not in hits:\n'
     '            hits.append(tag)\n'
     '    return hits',
     '    return []',
     [C + "test_manipulation_detected",
      C + "test_contamination_recorded_in_pair_log"]),
    # 两个分支必须一起写反：只反一个会被 agree 分歧检测抓到，那个校验点
    # 验的是别的东西（位置偏差），不会覆盖到"整体写反"这一形态。
    ("B 胜负映射两个分支一起写反", ENGINE,
     '                    if not flip:                      # 顺序1：A=对照组\n'
     '                        return "baseline" if w == "A" else "distilled"\n'
     '                    return "distilled" if w == "A" else "baseline"  '
     '# 顺序2：A=蒸馏体',
     '                    if not flip:\n'
     '                        return "distilled" if w == "A" else "baseline"\n'
     '                    return "baseline" if w == "A" else "distilled"',
     [C + "test_winner_matches_scores",
      C + "test_winner_baseline_when_baseline_wins"]),
    ("D 两侧全败仍按 0:0 计入", ENGINE,
     '                    counted = False',
     '                    counted = True',
     [C + "test_failed_case_not_counted_as_zero_zero"]),
    ("E 用例数每代重置", ENGINE,
     '        self.arena_cases += len(pairs)',
     '        self.arena_cases = len(pairs)',
     [C + "test_case_count_accumulates_across_generations"]),
    ("F 去偏判定恒真", ENGINE,
     '            debiased = o1 is not None and o2 is not None',
     '            debiased = True',
     [C + "test_undebiasable_run_cannot_claim_stronger",
      C + "test_failed_case_not_counted_as_zero_zero"]),
    # 两侧都要撤：只撤一侧时 judge 提示词里仍有分隔标记，"边界标记是否
    # 真的进了提示词"那条断言照样通过，测不到接线。
    ("G judge 提示词不降格为数据", ENGINE,
     '        prompt = (base_prompt\n'
     '                  .replace("<<ANSWER_A>>",\n'
     '                           wrap_candidate(ans_a, "候选答案", tag_a))\n'
     '                  .replace("<<ANSWER_B>>",\n'
     '                           wrap_candidate(ans_b, "候选答案", tag_b)))',
     '        prompt = (base_prompt\n'
     '                  .replace("<<ANSWER_A>>", ans_a)\n'
     '                  .replace("<<ANSWER_B>>", ans_b))',
     [C + "test_fence_injected_into_judge_prompt"]),
    # 分隔标记里写死位号：同一份内容在两个顺序里得到不同包裹，等于又添
    # 一层位置依赖——刚修好位置偏差，又从边界标记这边漏回来。
    ("H 分隔标记带位号", PROV,
     '    return (\n'
     '        f"{_CAND_BEGIN} source={source} trusted=no ---\\n"',
     '    return (\n'
     '        f"{_CAND_BEGIN} source=ANSWER A trusted=no ---\\n"',
     [C + "test_fence_has_no_position_marker"]),
    # 结束标记之后留内容：任何"见到第一个结束标记就当块结束"的解析器会
    # 把残留当成可信内容，比不包更糟——它给了虚假安全感。
    ("I 结束标记后留残留", PROV,
     '        f"---\\n{body}\\n---\\n{_CAND_END}"',
     '        f"---\\n{body}\\n---\\n{_CAND_END}\\ntrailing"',
     [C + "test_candidate_fence_is_exactly_one_pair"]),
    ("J 操纵检测恒命中（反向）", PROV,
     '    norm = normalize(text)\n'
     '    hits = []\n'
     '    for tag, rx in _JUDGE_COMPILED:\n'
     '        if rx.search(norm) and tag not in hits:\n'
     '            hits.append(tag)\n'
     '    return hits',
     '    return [t for t, _ in _JUDGE_COMPILED]',
     [C + "test_normal_answers_not_flagged",
      C + "test_clean_run_not_contaminated"]),
    ("K 分歧不报", ENGINE,
     '                winner = w1 if agree else "disagreement"',
     '                winner = w1',
     [C + "test_position_bias_reported_as_disagreement"]),
    ("L 零用例分支被摘", ENGINE,
     '        elif self.arena_cases <= 0:\n'
     '            # "至少评估过一个用例"这一条同样必须有对应分支。结论判定不该\n'
     '            # 依赖"恰好走不到"来保证——真走到时，用户拿到的是一个"结论不\n'
     '            # 成立且无原因"的产物。\n'
     '            return (\n'
     '                "当前没有完成任何用例的对照评测，「更强」没有支撑："\n'
     '                "请检查预算是否足以跑完至少一轮对照，或稍后重试。")',
     '',
     [C + "test_report_ratio_is_readable"]),
    # 注入必须落在用例实际走到的那条路径上：替身 LLM 在出题阶段返回的
    # 不是 JSON，_chat_json 解析失败后走的是"内置考题"分支，出题来源由
    # 该分支赋值。改另外两条（自动出题成功/失败）时该用例全绿，注入
    # 等于没生效——症状是"用例全绿"，与"守卫失效"长得一模一样。
    ("M 出题来源不落产物", ENGINE,
     '            self.question_source = "builtin"\n'
     '            cases = [{"id": f"c{i+1}", "input": t,',
     '            self.question_source = ""\n'
     '            cases = [{"id": f"c{i+1}", "input": t,',
     [C + "test_engine_records_adjudication_chain"]),
    ("C 基线", None, None, None, []),
]


def _restore(target: Path, backup: Path):
    shutil.move(str(backup), str(target))


def self_heal():
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。备份与被注入文件同目录，不放在 /tmp：后者在
    命令之间会被回收，备份没了就无法还原。
    """
    for p in (PROV, ENGINE):
        bak = Path(str(p) + ".bak")
        if bak.exists():
            _restore(p, bak)
            print(f"  [自愈] 还原 {p.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="",
                    help="只跑指定前缀的校验点，逗号分隔")
    args = ap.parse_args()
    picks = [s.strip() for s in args.only.split(",") if s.strip()]

    self_heal()
    bad = []
    ran = 0
    for name, target, old, new, expect in ANCHORS:
        if picks and not any(name.startswith(s) for s in picks):
            continue
        ran += 1
        if target is None:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        src = target.read_text(encoding="utf-8")
        n = src.count(old)
        if n != 1:
            print(f"  [未抓到] {name}：锚点命中 {n} 次，须重定位")
            bad.append(name)
            continue
        bak = Path(str(target) + ".bak")
        shutil.copy2(target, bak)
        try:
            target.write_text(src.replace(old, new, 1), encoding="utf-8")
            if target.read_text(encoding="utf-8") == src:
                print(f"  [未抓到] {name}：写入没生效，注入未落盘")
                bad.append(name)
                continue
            rc, failed, tail = pytest_run(TESTS)
        finally:
            _restore(target, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why}）")
        if failed:
            print(f"      失败项 {failed}")
        if not ok:
            bad.append(name)
        # 还原后必须回绿：否则本轮结果是在脏代码上跑出来的。
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    uniq = list(dict.fromkeys(bad))
    print(f"本次校验点 {ran} 个，抓到 {ran - len(uniq)} 个")
    for n in uniq:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
