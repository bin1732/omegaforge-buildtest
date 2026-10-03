#!/usr/bin/env python3
"""相关部分校验：撤掉修复，守卫必须变红。

每个校验点：
    1. 备份原文
    2. 注入（精确字符串替换，要求命中且只命中一次）
    3. `ast.parse` 强制校验语法（防止"测试根本没跑起来"被误读成"抓到"）
    4. 跑守卫，解析真实 failed 数
    5. 还原并校验与备份逐字节一致

判据必须是"有真实 failed"——rc!=0 也可能是 pytest 自身报错（例如
usage error），那一条用例都没执行。
"""

import ast
import io
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("PY", sys.executable)
ENV = dict(os.environ, PYTHONPATH="/data/workspace/.pylib:" + REPO,
           OMEGAFORGE_MOCK="1")

TESTS = "tests/test_baseline_wiring.py"
C = TESTS + "::"

# (校验点名, 目标文件, 原串, 注入串)
ANCHORS = [
    ("A 撤掉 provided 够格判（洗白通道复原）",
     "omegaforge/domain/baseline.py",
     '        if not _enough(given):',
     '        if False:',
     [C + "test_short_boundary",
      C + "test_short_provided_cannot_whiten_verdict"]),

    ("B 撤掉引擎转发（入口传了也白发）",
     "omegaforge/distill/engine.py",
     '            self.baseline = build_baseline(\n'
     '                sig, provided_prompt=self.baseline_prompt)',
     '            self.baseline = build_baseline(sig)',
     [C + "test_distill_forwards_baseline_prompt",
      C + "test_http_end_to_end_becomes_comparable"]),

    ("C 撤掉 CLI --baseline-prompt",
     "omegaforge/cli.py",
     '    d.add_argument("--baseline-prompt", default="",',
     '    d.add_argument("--baseline-prompt-X", default="",',
     [C + "test_cli_has_baseline_prompt_option"]),

    ("D 撤掉 MCP schema 的 baseline_prompt",
     "omegaforge/mcp_server.py",
     '             "baseline_prompt": {"type": "string",',
     '             "baseline_prompt_X": {"type": "string",',
     [C + "test_mcp_schema_has_baseline_prompt"]),

    ("E 撤掉 server 读取 baseline_prompt",
     "omegaforge/server.py",
     '        baseline_prompt = as_text(payload, "baseline_prompt", "",',
     '        baseline_prompt = as_text(payload, "baseline_prompt_X", "",',
     [C + "test_http_end_to_end_becomes_comparable"]),

    ("F 撤掉 server 转发给 engine",
     "omegaforge/server.py",
     '                                        baseline_prompt=baseline_prompt)',
     '                                        baseline_prompt="")',
     [C + "test_http_end_to_end_becomes_comparable"]),
]


sys.path.insert(0, REPO)
from probes._rev_verdict import pytest_run, verdict  # noqa: E402


def run_guard():
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数说明不了失败的是不是预期那一条。
    """
    return pytest_run(TESTS)


def main() -> int:
    print("=" * 74)
    print("第 45 章反向验证：对照组补救通道")
    print("=" * 74)

    base_rc, base_f, base_tail = run_guard()
    print(f"\n[基线] 未注入: rc={base_rc} failed={len(base_f)}  {base_tail}")
    if base_rc != 0:
        print("基线不干净，先修好再验")
        return 2

    caught = 0
    broken = 0
    for name, rel, old, new, expect in ANCHORS:
        path = os.path.join(REPO, rel)
        orig = io.open(path, encoding="utf-8").read()
        if orig.count(old) != 1:
            # 校验点没跑 ≠ 校验点跑了没抓到。跳过会把"校验点失效"静默吞掉，
            # 让人把"没验过"读成"验过且未抓到"（本项目已栽过一次）。
            print(f"\n[{name}] ✗ 锚点失效：注入点不唯一 count={orig.count(old)}")
            broken += 1
            continue
        try:
            inj = orig.replace(old, new)
            ast.parse(inj)          # 语法硬校验
            io.open(path, "w", encoding="utf-8").write(inj)
            rc, failed, tail = run_guard()
            ok, why = verdict(rc, failed, expect)
            caught += 1 if ok else 0
            print(f"\n[{name}]  {'抓到' if ok else '未抓到'}（{why}）")
        finally:
            io.open(path, "w", encoding="utf-8").write(orig)
        now = io.open(path, encoding="utf-8").read()
        assert now == orig, f"还原失败: {rel}"

    print("\n" + "=" * 74)
    print(f"反向验证: {caught}/{len(ANCHORS)} 锚点抓到"
          + (f"，{broken} 个锚点失效（未执行，不算抓到）" if broken else ""))
    print("=" * 74)
    # 校验点失效必须计入失败：否则"没验过"会被当成"验过且未抓到"蒙混过关。
    return 0 if (caught + broken == len(ANCHORS) and broken == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
