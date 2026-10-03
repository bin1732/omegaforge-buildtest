#!/usr/bin/env python3
"""流式中断守卫的校验：确认四条守卫不是空转。

校验点：
  A 落库时不带中断标记        → 半截回复应以完整回答的样式呈现，必须被抓到
  B 客户端断开仍记进错误日志  → 断开被当故障，必须被抓到
  C 断开后不落库              → 界面上显示过的文字凭空消失，必须被抓到
  D 完整回复也标中断          → 防处理过头，必须被抓到

每个校验点：注入 → 强制语法校验 → 跑守卫 → 还原 → 对照（必须全绿）。
注入点命中次数不为 1 一律判失败，不当 SKIP——"校验点没跑"和"校验点跑了没
抓到"是两回事，混淆会得出假结论。
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = "tests/test_stream_disconnect.py"
C = GUARD + "::"
SERVER = "omegaforge/server.py"

ANCHORS = [
    ("A 落库时不带中断标记", SERVER,
     "                            CONVS.add_message(cid, \"assistant\", reply,\n"
     "                                              model=chosen, interrupted=True)",
     "                            CONVS.add_message(cid, \"assistant\", reply,\n"
     "                                              model=chosen)",
     [C + "test_01_中断后仍落库且带标记"]),
    ("B 客户端断开仍记进错误日志", SERVER,
     "                except _Disconnected:\n"
     "                    # 客户端已断开：不再尝试兜底（推不出去），也不记错误日志。\n"
     "                    interrupted = True",
     "                except _Disconnected:\n"
     "                    interrupted = True\n"
     "                    user_error(BrokenPipeError(), \"chat_stream\")",
     [C + "test_02_客户端断开不记进错误日志"]),
    ("C 断开后不落库", SERVER,
     "                    if reply:\n"
     "                        try:\n"
     "                            CONVS.add_message(cid, \"assistant\", reply,",
     "                    if False:\n"
     "                        try:\n"
     "                            CONVS.add_message(cid, \"assistant\", reply,",
     [C + "test_01_中断后仍落库且带标记"]),
    ("D 完整回复也标中断", SERVER,
     "                conv = CONVS.add_message(cid, \"assistant\", reply, model=chosen)",
     "                conv = CONVS.add_message(cid, \"assistant\", reply, "
     "model=chosen, interrupted=True)",
     [C + "test_04_正常跑完不带中断标记"]),
]


sys.path.insert(0, REPO)
from probes._rev_verdict import pytest_run, verdict  # noqa: E402


def run_guard():
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数说明不了失败的是不是预期那一条。
    """
    return pytest_run(GUARD)


def main():
    results = []
    broken = 0
    for name, rel, old, new, expect in ANCHORS:
        path = os.path.join(REPO, rel)
        orig = io.open(path, encoding="utf-8").read()
        if orig.count(old) != 1:
            print(f"\n[{name}] ✗ 锚点失效：注入点命中 {orig.count(old)} 次")
            results.append((name, "锚点失效", -1))
            broken += 1
            continue
        try:
            inj = orig.replace(old, new, 1)
            ast.parse(inj)          # 语法硬校验：否则"没跑起来"会被当成"抓到"
            io.open(path, "w", encoding="utf-8").write(inj)
            rc, failed, tail = run_guard()
            ok, why = verdict(rc, failed, expect)
            print(f"\n[{name}]  {'抓到' if ok else '未抓到'}（{why}）")
            results.append((name, "抓到" if ok else "未抓到", len(failed)))
        finally:
            io.open(path, "w", encoding="utf-8").write(orig)

    rc, _f, tail = run_guard()
    n = rc
    print(f"\n[对照] 全部还原后 rc={rc}  {tail}")

    print("\n" + "=" * 72)
    caught = sum(1 for _, s, _ in results if s == "抓到")
    print(f"反向验证: {caught}/{len(results)} 锚点抓到"
          + (f"，{broken} 个锚点失效（未执行，不算抓到）" if broken else ""))
    for nm, s, _ in results:
        print(f"  {s}  {nm}")
    print("=" * 72)
    return 0 if (caught == len(results) and n == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
