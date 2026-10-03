#!/usr/bin/env python3
"""蒸馏引擎契约守卫的判定校验：撤掉修复，指定的用例必须变红。

## 为什么要点名到用例

只比对"有没有失败项"区分不了两件事：失败的是不是这一处守卫对应的那条
用例。给被测模块加一个模块级未定义名，整片用例都会失败，任何"有失败"
的判定都会被满足——那时的红是环境故障，不是守卫在起作用。

因此每个校验点都点名预期变红的用例，并且严格：出现预期之外的失败项同样
判未抓到。

## 几条守卫共用同一个收敛函数，点名必须分开

``_avg_scores`` 同时承载"只除真正的数字维度"与"bool 不算分数"两条约束，
撤掉前者会连带后者所在的用例一起红。若把两条合成一个校验点，撤掉其中
一条时另一条失效无从分辨——因此分别注入、分别点名。

## 点名的来源

每个点名清单都要逐个注入确认。照着注入点推出来的清单会漏掉连带失败的
那几条，而漏掉的后果是：注入生效了，判定却说"预期之外还失败"。

用法::

    python3 probes/rev_engine_contract.py            # 全部校验点
    python3 probes/rev_engine_contract.py A B        # 只跑指定校验点
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT", "/data/workspace/LATEST"))
sys.path.insert(0, str(ROOT / "probes"))

from _rev_verdict import pytest_run, reg_add, restore_src, verdict  # noqa: E402

SRC = ROOT / "omegaforge" / "distill" / "engine.py"
TESTS = "tests/test_engine_contract.py"
BACKUP = Path("/data/workspace/.rev_backups/engine.py.bak")


def case(name: str) -> str:
    return f"{TESTS}::{name}"


# (标识, 说明, [(注入前, 注入后)], 预期变红的用例, 是否反向)
# 反向校验点（reverse=True）要求判定为"未抓到"：用来证明判定助手没被
# 环境故障骗过——注入与守卫无关时，任何点名都不该被满足。
ANCHORS: list[tuple[str, str, list[tuple[str, str]], list[str], bool]] = [
    ("A", "评分按容器维度数相除（非数字维度稀释分数）", [
        ("    return sum(vals) / len(vals)",
         "    return sum(vals) / max(1, len(raw))"),
        # bool 被排除后 vals 与 raw 长度不同，同一条用例一并红，属自然连带。
    ], [case("test_partial_numeric_scores_not_diluted"),
        case("test_avg_scores_rejects_non_dict_and_bool")], False),

    ("B", "bool 计入分数（True 被当成 1 分）", [
        ("    vals = [v for v in raw.values()\n"
         "            if isinstance(v, (int, float)) "
         "and not isinstance(v, bool)]",
         "    vals = [v for v in raw.values()\n"
         "            if isinstance(v, (int, float))]"),
    ], [case("test_avg_scores_rejects_non_dict_and_bool")], False),

    ("C", "scores 整体不是对象时不兜底", [
        ("        scores = scores if isinstance(scores, dict) else {}",
         "        scores = scores"),
    ], [case("test_scores_container_non_dict_does_not_crash")], False),

    ("D", "胜出分支不写评审理由", [
        ("            report.judge_reasons = reasons",
         "            if not (d_avg > b_avg + 0.25):\n"
         "                report.judge_reasons = reasons"),
    ], [case("test_judge_reasons_present_on_win")], False),

    ("E", "持平分支不写评审理由", [
        ("            report.judge_reasons = reasons",
         "            if not (b_avg - 0.25 <= d_avg <= b_avg + 0.25):\n"
         "                report.judge_reasons = reasons"),
    ], [case("test_judge_reasons_present_on_tie")], False),

    ("F", "字符串 deltas 被逐字符拆开", [
        ("    if isinstance(raw, str):\n"
         "        return [raw.strip()] if raw.strip() else []",
         "    if isinstance(raw, str):\n"
         "        return list(raw) if raw.strip() else []"),
    ], [case("test_string_deltas_not_split_into_chars")], False),

    ("G", "token 估算不收敛（负数与字符串进流程）", [
        ("    try:\n        n = int(raw)\n"
         "    except (TypeError, ValueError):\n        return 0\n"
         "    return max(0, n)",
         "    n = int(raw)\n    return n"),
    ], [case("test_token_count_coerced")], False),

    ("H", "name 为空时不兜底（产物泄漏 None）", [
        ("            name=(_as_gene_list([data.get(\"name\")]) or\n"
         "                  _as_gene_list([spec.name]) or "
         "[\"DistilledAgent\"])[0],",
         "            name=data.get(\"name\", spec.name),"),
    ], [case("test_null_name_does_not_crash_or_leak_none")], False),

    ("I", "system_prompt 非字符串时不收敛", [
        ("        g.system_prompt = sp if isinstance(sp, str) else (\n"
         "            json.dumps(sp, ensure_ascii=False) "
         "if isinstance(sp, (dict, list))\n"
         "            else \"\")",
         "        g.system_prompt = sp"),
    ], [case("test_system_prompt_non_string_is_coerced")], False),

    # -------- 判定助手自身的自检 --------
    ("X", "无关故障不得被当成抓到（模块级未定义名）", [
        ("from __future__ import annotations",
         "from __future__ import annotations\n_unrelated_undefined_name_"),
    ], [], True),
]


def main() -> int:
    only = set(sys.argv[1:])
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    orig = SRC.read_text(encoding="utf-8")

    caught = 0
    ran = 0
    for name, desc, subs, expect, reverse in ANCHORS:
        if only and name not in only:
            continue
        ran += 1
        cur = orig
        hit = True
        for old, new in subs:
            if old not in cur:
                hit = False
                break
            cur = cur.replace(old, new, 1)
        if not hit:
            print(f"  [未抓到] {name} {desc} —— 注入点不存在，锚点须重定位")
            continue
        # 备份必须在注入之前写：若备份晚于注入，被中断后备份里存的是已注入
        # 版本，还原回到的是脏代码，而此后所有校验点都在这份脏代码上跑。
        BACKUP.write_text(orig, encoding="utf-8")
        # 登记必须在改源码之前：中断后靠它把源码对齐回去
        reg_add(SRC, BACKUP)
        SRC.write_text(cur, encoding="utf-8")
        try:
            rc, failed, tail = pytest_run(TESTS)
        finally:
            restore_src(SRC, BACKUP)
        got, why = verdict(rc, failed, expect)
        ok = (not got) if reverse else got
        caught += int(ok)
        label = "抓到" if ok else "未抓到"
        print(f"  [{label}] {name} {desc}: {why}")
        if not ok and reverse:
            print("        反向校验点要求判定为未抓到，实际判成抓到")
    print(f"\n{caught}/{ran} 抓到")

    rc, failed, tail = pytest_run(TESTS)
    print(f"  基线：{tail}")
    if rc != 0:
        print(f"  基线不通过：{failed[:5]}")
        return 1
    return 0 if caught == ran else 1


if __name__ == "__main__":
    sys.exit(main())
