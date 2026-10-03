#!/usr/bin/env python3
"""MCP 数值边界与目录名守卫的判定校验。

## 为什么补这个脚本

`tests/test_mcp_limits.py`（26 项）覆盖四类可复现缺陷：MCP 是三个入口里唯一
没接 limits.py 的、默认值曾与 limits.py 各一套、source 折目录名时 `..` 原样
保留导致产物写到用户数据根目录、外部蒸馏每次重新出题无法跨次比较。文件说明
里逐条写了复现方式，但仓库里没有脚本引用过它。本脚本把"能复现"换成可执行、
可重跑的证据。

## 三个锚点对应三条互不覆盖的路径

 * A 数值边界退回裸 int：同一批输入在 CLI 与 server 被拒、在 MCP 被放行
   ——`rounds=0` 得到空转产物却返回成功，`rounds=10**9` 是十亿轮评测，
   `budget=-1` 把 TokenBank 压成 0。注入后 6 条变红。
 * B 目录名只替换斜杠：`..` 原样保留，`source=".."` 把产物写到 mcp_runs 的
   父目录（用户数据根目录），覆盖与本次蒸馏无关的文件。注入后 3 条变红。
 * C 评测集直接收下原始值：接受文件路径等于让外部模型指定本机任意文件去读，
   而 MCP 的调用方比 HTTP 侧更不可信。注入后 2 条变红。

三条分属数值、路径、输入形态三个面，互相证不了对方。

`test_path_string_rejected` 不在 C 的点名里：它直接调 `_normalize_eval_set`，
不走 `call_tool`，因此 C 注入不到它。点名一律取自实际执行结果，判定用严格
模式——出现预期之外的失败项同样判未抓到。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_mcp_limits.py"
C = TESTS + "::"
MCP = ROOT / "omegaforge" / "mcp_server.py"

ANCHORS = [
    ("A 数值边界退回裸 int", MCP,
     '            budget = as_int(args, "budget", DEFAULT_BUDGET,\n'
     "                            minimum=MIN_BUDGET, maximum=MAX_BUDGET,\n"
     '                            label="预算")\n'
     '            rounds = as_int(args, "rounds", ROUNDS_DEFAULT,\n'
     "                            minimum=ROUNDS_MIN, maximum=ROUNDS_MAX,\n"
     '                            label="评测轮数")\n'
     '            gens = as_int(args, "gens", GENS_DEFAULT,\n'
     "                          minimum=GENS_MIN, maximum=GENS_MAX,\n"
     '                          label="进化代数")',
     '            budget = int(args.get("budget", DEFAULT_BUDGET))\n'
     '            rounds = int(args.get("rounds", ROUNDS_DEFAULT))\n'
     '            gens = int(args.get("gens", GENS_DEFAULT))',
     [C + "test_rounds_zero_rejected",
      C + "test_gens_zero_rejected",
      C + "test_negative_rounds_rejected",
      C + "test_rounds_upper_bound",
      C + "test_gens_upper_bound",
      C + "test_budget_upper_and_lower_bound"]),
    ("B 目录名只替换斜杠", MCP,
     '    safe = "".join(c if c.isalnum() or c in "._-" else "_"\n'
     '                   for c in str(source))[:40].strip("._ ")',
     '    safe = str(source).replace("/", "_").replace("\\\\", "_")[:40]',
     [C + "test_parent_traversal_flattened",
      C + "test_separators_removed",
      C + "test_output_stays_under_mcp_runs"]),
    ("C 评测集直接收下原始值", MCP,
     "            eval_set = (DistillEngine._normalize_eval_set(raw_eval)\n"
     "                        if raw_eval is not None else None)",
     "            eval_set = raw_eval",
     [C + "test_empty_set_not_silently_ignored",
      C + "test_missing_input_rejected_with_chinese"]),
    ("D 基线", None, None, None, []),
]


def _restore(target: Path, backup: Path):
    shutil.move(str(backup), str(target))


def self_heal():
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。备份与被注入文件同目录，不放在 /tmp：后者在
    命令之间会被回收，备份没了就无法还原。
    """
    bak = Path(str(MCP) + ".bak")
    if bak.exists():
        _restore(MCP, bak)
        print(f"  [自愈] 还原 {MCP.name}")


def main() -> int:
    self_heal()
    bad = []
    for name, target, old, new, expect in ANCHORS:
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
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:80]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    print(f"校验点 {len(ANCHORS)} 个，抓到 {len(ANCHORS) - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
