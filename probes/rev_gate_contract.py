#!/usr/bin/env python3
"""门禁契约守卫的判定校验。

## 六个锚点对应六条互不覆盖的路径

 * A 命令级危险判定失效：`rm -rf /` 与 fork bomb 放行 → 4 条变红。
 * B critical 清单失效：配置注入类命令放行 → 4 条变红。
 * C 审批凭证可重放：同一凭证第二次仍放行 → 1 条变红。
 * D 审批不绑定参数摘要：换条命令沿用同一凭证 → 1 条变红。
 * E 幂等键不生效：同键重放执行第二遍 → 1 条变红。
 * F 审计坏行不跳过：一行坏记录让整个查询失败 → 1 条变红。

## A 与 B 为什么必须分开

两层判定的拦截对象不重叠：命令级判定认的是破坏性字样与结构（删除、覆写
设备、fork 炸弹），critical 清单认的是"通过配置注入让程序去执行另一条
命令"（`git config core.pager` 这类）。后者不含任何破坏性字样，命令级判定
管不到；前者也不是配置注入，critical 清单管不到。

只撤一层时，另一层照常拦，红的非对应那一组。因此两个锚点各自单独验，
合并成一个校验点会漏掉其中一层的失效。

## B 为什么需要一条专属用例

critical 清单若没有只由它拦住的命令作为用例，撤掉它时这层失效不会体现为
任何一条用例变红：破坏性命令由命令级判定拦住，与 critical 清单无关。
`test_config_escape_denied_in_every_mode` 用的命令不含破坏性字样，是
critical 清单独有的拦截对象。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。同一文件多处替换时只备份一次，否则后一次备份覆盖
前一次，还原回来的是已注入的版本。
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

TESTS = "tests/test_gate_contract.py"
C = TESTS + "::"
ST = ROOT / "omegaforge" / "tools" / "system_tools.py"
POL = ROOT / "omegaforge" / "tools" / "policy.py"

ANCHORS = [
    ("A 命令级危险判定失效",
     [(ST, "        if _dangerous(cmd):", "        if False:")],
     [C + "test_critical_denied_in_every_mode"]),
    ("B critical 清单失效",
     [(ST, "        why = critical_check(tool, args)", "        why = None")],
     [C + "test_config_escape_denied_in_every_mode"]),
    ("C 审批凭证可重放",
     [(POL, "    seen = _load_used(now)\n"
            "    if nonce in seen:\n"
            "        return False\n"
            "    seen[nonce] = exp",
       "    seen = _load_used(now)\n"
       "    seen[nonce] = exp")],
     [C + "test_approval_one_shot_and_replay_rejected"]),
    ("D 审批不绑定参数摘要",
     [(POL, '    body = f"{tool}|{dig}|{exp}|{nonce}"',
       '    body = f"{tool}|{exp}|{nonce}"'),
      (POL, '    body = f"{tool}|{_args_digest(args)}|{exp}|{nonce}"',
       '    body = f"{tool}|{exp}|{nonce}"')],
     [C + "test_approval_bound_to_args"]),
    ("E 幂等键不生效",
     [(ST, "    def run_command(self, cmd: str, timeout: int = 20,\n"
           "                    approval: Optional[str] = None,\n"
           "                    idem_key: Optional[str] = None) -> dict:\n"
           '        hit = idem_get(idem_key or "")',
       "    def run_command(self, cmd: str, timeout: int = 20,\n"
       "                    approval: Optional[str] = None,\n"
       "                    idem_key: Optional[str] = None) -> dict:\n"
       "        hit = None")],
     [C + "test_idempotency_replay_runs_once"]),
    ("F 审计坏行不跳过",
     [(POL, "        except json.JSONDecodeError:\n"
            "            # 坏行跳过而不是让整个审计查询 500（与 UsageStore 同策略）\n"
            "            continue",
       "        except json.JSONDecodeError:\n"
       "            raise")],
     [C + "test_audit_bad_line_does_not_500"]),
    ("G 基线", [], []),
]

FILES = [ST, POL]


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
    bad = []
    for name, edits, expect in ANCHORS:
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次，须重定位")
                bad.append(name)
                break
            if path not in paths:
                paths.append(path)
        else:
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
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:80]}）")
            if not ok:
                bad.append(name)
            rc2, _, tail2 = pytest_run(TESTS)
            if rc2 != 0:
                print(f"      还原后仍非全绿：{tail2}")
                bad.append(name + "（还原失败）")
            continue
        continue

    print("\n=== 汇总 ===")
    print(f"校验点 {len(ANCHORS)} 个，抓到 {len(ANCHORS) - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
