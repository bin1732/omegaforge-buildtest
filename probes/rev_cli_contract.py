#!/usr/bin/env python3
"""CLI 契约守卫的判定校验。

## 九个校验点：八个撤防线 + 一个防修过头

 * A 撤掉 rounds/gens 闸门：评测轮次与进化代数的下限不再生效 → 2 条。
 * B 撤掉 budget 闸门：预算下限不再生效 → 1 条。
 * C 失败出口返回 0（静默成功）：11 条。
 * D 日志目录写死当前工作目录：不再跟随数据目录 → 2 条。
 * E 日志落盘失败时抛出而非降级：启动被阻断 → 1 条。
 * F UserError 不再原样透出（防修过头）：闸门文案被泛化 → 3 条。
 * G 损坏 genome 的报错甩锅模型 → 1 条。
 * H MCP 撤掉显式日志接线 → 1 条。
 * I 基线。

## B 与 H 的判据为什么写成这样

两条断言若只验表面条件，注入后一条都不会红——下面各条说明它容易被
哪条别的路径满足，因此判据必须排除那一条：

 * B 只断言 rc!=0 与提示含"预算"。预算为 0 时即使闸门撤掉，引擎也会以
   预算耗尽退出——退出码非零、提示同样带"预算"二字，于是"闸门拦的"和
   "跑起来才失败的"在断言上完全等价。已加"不许跑进引擎"这一条。
 * H 断言日志目录存在。而转译层有惰性兜底，任何出错请求都会顺手把日志
   目录建出来，于是入口有没有显式接通都成立。已改为只发 initialize。

## F 为什么是反向的

只验"撤掉修复会变红"证明不了修复刚好够用。F 让 UserError 也不再透出、
一律走转译映射：此时闸门仍在拦，但拦下后给的是泛化文案，用户看不出该
改哪个参数。这类失效不会被任何"撤掉修复"的锚点抓到。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。同一文件多处替换时只备份一次，否则后一次备份
覆盖前一次，还原回来的是已注入的版本。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_selfheal import (
    dirty_names,
    report_and_stop,
)
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_cli_contract.py"
C = TESTS + "::"
CLI = ROOT / "omegaforge" / "cli.py"
ERR = ROOT / "omegaforge" / "core" / "errors.py"
MCP = ROOT / "omegaforge" / "mcp_server.py"

GATE_RG = ('    args.rounds = as_int(payload, "rounds", ROUNDS_DEFAULT,\n'
           '                         minimum=ROUNDS_MIN, maximum=ROUNDS_MAX,\n'
           '                         label="评测轮次")\n'
           '    args.gens = as_int(payload, "gens", GENS_DEFAULT,\n'
           '                       minimum=GENS_MIN, maximum=GENS_MAX,\n'
           '                       label="进化代数")')
GATE_RG_OFF = ('    args.rounds = args.rounds\n'
               '    args.gens = args.gens')

GATE_B = ('    args.budget = as_int(payload, "budget", DEFAULT_BUDGET,\n'
          '                         minimum=MIN_BUDGET, maximum=MAX_BUDGET,\n'
          '                         label="预算")')
GATE_B_OFF = '    args.budget = args.budget'

FAIL_RET = '    print(f"⚠ {msg}", file=sys.stderr)\n    return 1'
FAIL_RET_0 = '    print(f"⚠ {msg}", file=sys.stderr)\n    return 0'

LOGDIR = ('    home = os.getenv("OMEGAFORGE_HOME") or '
          'os.path.join(os.getcwd(), ".omegaforge")\n'
          '    return os.path.join(home, "logs")')
LOGDIR_FIXED = '    return os.path.join(os.getcwd(), "logs")'

CONF_DEGRADE = ('    except OSError:\n'
                '        # 目录建不了或文件开不了：降级为不落盘，仅保留调用方的既有行为。\n'
                '        # 不向上抛——日志是旁路，不该变成启动失败的原因。\n'
                '        return ""')
CONF_RAISE = '    except OSError:\n        raise'

UE_ISO = '    if isinstance(exc, UserError):\n        return exc.message'
UE_ISO_OFF = '    if False:\n        return exc.message'

GENOME_MSG = ('            return _fail("该文件不是有效的 genome（内容不是合法 JSON），"\n'
              '                         "请重新导出后再试")')
GENOME_MSG_OLD = '            return _fail("模型返回了无法解析的内容")'

MCP_LOG = '    configure_logging()'
MCP_LOG_OFF = '    pass'

GATE_RG_T = C + "test_cli_gate_rejects_zero_rounds_and_gens"
GATE_B_T = C + "test_cli_gate_rejects_tiny_budget"
LOGDIR_T = C + "test_error_log_follows_data_home"
MCP_LOG_T = C + "test_mcp_error_log_lands_in_data_home"
GENOME_T = C + "test_corrupt_genome_message_does_not_blame_model"

SILENT = [GATE_B_T, GATE_RG_T, GENOME_T,
          C + "test_corrupt_report_json_is_reported_as_file_problem",
          C + "test_no_english_strings_leak",
          C + "test_no_traceback_leaks_to_user_output",
          C + "test_report_missing_dir_is_not_silent_success",
          C + "test_run_missing_genome_is_not_silent_success",
          C + "test_task_done_empty_is_not_silent_success"]

ANCHORS = [
    ("A rounds/gens 闸门失效", [(CLI, GATE_RG, GATE_RG_OFF)], [GATE_RG_T]),
    ("B budget 闸门失效", [(CLI, GATE_B, GATE_B_OFF)], [GATE_B_T]),
    ("C 失败出口返回 0（静默成功）", [(CLI, FAIL_RET, FAIL_RET_0)], SILENT),
    ("D 日志目录不跟随数据目录", [(ERR, LOGDIR, LOGDIR_FIXED)],
     [LOGDIR_T, MCP_LOG_T]),
    ("E 日志落盘失败时抛出（防修过头）", [(ERR, CONF_DEGRADE, CONF_RAISE)],
     [C + "test_configure_logging_returns_empty_on_uncastable_dir"]),
    ("F UserError 不再透出（防修过头）", [(ERR, UE_ISO, UE_ISO_OFF)],
     [GATE_B_T, GATE_RG_T]),
    ("G 损坏 genome 甩锅模型", [(CLI, GENOME_MSG, GENOME_MSG_OLD)], [GENOME_T]),
    ("H MCP 撤掉显式日志接线", [(MCP, MCP_LOG, MCP_LOG_OFF)], [MCP_LOG_T]),
    ("I 基线", [], []),
]

FILES = [CLI, ERR, MCP]


def _restore(path: Path, bak: Path):
    shutil.move(str(bak), str(path))


def self_heal() -> bool:
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。

    但"残留"与"编写中的改动"在差异清单里完全同形，一律用 HEAD 覆盖会把
    当次正在写的源码一并抹掉。故这里只还原本脚本留下的 .bak，仍有差异时
    中止并交给人判断。
    """
    for path in FILES:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            _restore(path, bak)
            print(f"  [自愈] 还原 {path.name}")
    names = dirty_names(FILES, ROOT)
    if names:
        return report_and_stop(names)
    return True


def main() -> int:
    if not self_heal():
        return 1
    want = sys.argv[1:]
    bad = []
    for name, edits, expect in ANCHORS:
        if want and name[0] not in want:
            continue
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        missing = False
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次，"
                      f"须重定位（{path.name}）")
                bad.append(name)
                missing = True
                break
            if path not in paths:
                paths.append(path)
        if missing:
            continue

        baks = []
        try:
            for path in paths:
                bak = Path(str(path) + ".bak")
                shutil.copy2(path, bak)
                baks.append((path, bak))
            for path, old, new in edits:
                src = path.read_text(encoding="utf-8")
                path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed, tail = pytest_run(TESTS)
        finally:
            for path, bak in baks:
                if bak.exists():
                    _restore(path, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:60]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    ran = len([a for a in ANCHORS if not want or a[0][0] in want])
    print(f"本段校验点 {ran} 个，抓到 {ran - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
