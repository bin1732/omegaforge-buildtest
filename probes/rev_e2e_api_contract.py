#!/usr/bin/env python3
"""相关部分联调契约守卫的校验：撤掉修复，守卫必须变红。

设计要点（本项目栽过的坑都在这）：

1. 精确匹配期望：每个校验点声明"期望哪几条失败"。全红不等于抓到——
   注入把文件改崩、测试跑不起来，也是非 0 退出。
2. 因此每个校验点同时断言"对照组仍绿"：B 层撤前端键名时，另外两条契约
   用例必须照常通过，否则说明是全局崩溃而非真正的守卫生效。
3. 注入前备份、finally 无条件还原。上一章用 git checkout 还原时把自己
   刚写的修复一起还原掉了，这次走内存备份。
4. Python 侧注入后强制 ast.parse 校验语法。
"""

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())

import ast
import os
import subprocess
import sys

REPO = "/data/workspace/LATEST"
API = os.path.join(REPO, "frontend", "src", "lib", "api.ts")
SRV = os.path.join(REPO, "omegaforge", "server.py")

TEST = "tests/test_e2e_api_contract.py::"

# (校验点名, 文件, 原串, 注入串, 期望失败的用例, 必须仍通过的用例)
ANCHORS = [
    ("A 撤后端 kb/search 兼容（只认 q）", SRV,
     'pick_first(payload, ("q", "query")) or ""',
     'payload.get("q", "")',
     [TEST + "LegacyKeyStillAccepted::test_kb_search_query"],
     [TEST + "FrontendPayloadHitsBackend::test_kb_search_finds_seeded_doc"]),

    ("B 撤前端 kbSearch 键名（改回 query）", API,
     "'/api/kb/search', { q })",
     "'/api/kb/search', { query: q })",
     [TEST + "ContractKeyIsCanonical::test_kb_search_key"],
     # 互相掩盖实证：后端仍兼容，端到端照样绿——只有 B 层抓得到
     [TEST + "FrontendPayloadHitsBackend::test_kb_search_finds_seeded_doc",
      TEST + "ContractKeyIsCanonical::test_task_done_key",
      TEST + "LegacyKeyStillAccepted::test_kb_search_query"]),

    ("C 撤后端 tasks/done 兼容（只认 task_id）", SRV,
     'pick_first(payload, ("task_id", "id"))',
     'payload.get("task_id", "")',
     [TEST + "LegacyKeyStillAccepted::test_task_done_id"],
     [TEST + "FrontendPayloadHitsBackend::test_task_done_completes"]),

    ("D 撤前端 taskDone 键名（改回 id）", API,
     "'/api/tasks/done', { task_id: id })",
     "'/api/tasks/done', { id })",
     [TEST + "ContractKeyIsCanonical::test_task_done_key"],
     [TEST + "FrontendPayloadHitsBackend::test_task_done_completes",
      TEST + "ContractKeyIsCanonical::test_kb_search_key",
      TEST + "LegacyKeyStillAccepted::test_task_done_id"]),
]


def run(node_ids):
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header",
           "-p", "no:cacheprovider"] + list(node_ids)
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                       timeout=300)
    return p.returncode, p.stdout + p.stderr


def main():
    orig = {f: open(f, encoding="utf-8").read() for f in (API, SRV)}
    all_ok = True
    try:
        for name, path, old, new, expect_fail, must_pass in ANCHORS:
            src = orig[path]
            if old not in src:
                print(f"{name}: 注入点不存在 → 锚点无效（守卫未覆盖）")
                all_ok = False
                continue
            open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
            if path.endswith(".py"):
                try:
                    ast.parse(open(path, encoding="utf-8").read())
                except SyntaxError as e:
                    print(f"{name}: 注入后语法错误 {e} → 结果不可信")
                    all_ok = False
                    open(path, "w", encoding="utf-8").write(src)
                    continue

            rc_fail, _ = run(expect_fail)
            rc_pass, out = run(must_pass)
            caught = rc_fail != 0
            clean = rc_pass == 0
            ok = caught and clean
            all_ok &= ok
            print(f"{'抓到' if caught else '未抓到':<4}"
                  f"{'对照绿' if clean else '对照异常':<7}{name}")
            if not clean:
                print("   对照输出尾部：", out.strip().splitlines()[-1][:160])
            open(path, "w", encoding="utf-8").write(src)
    finally:
        for f, s in orig.items():
            open(f, "w", encoding="utf-8").write(s)
    # 还原核对
    for f, s in orig.items():
        if open(f, encoding="utf-8").read() != s:
            print(f"还原失败：{f}")
            all_ok = False
    print("REVERT_OK" if all_ok else "REVERT_FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
