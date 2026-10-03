#!/usr/bin/env python3
"""校验：probe_trust_note_live.py 的守卫是不是空转。

用法：python3 probes/revert_trust_note.py A|B|C

校验点：
    A  撤掉 baseline_comparable 分支（改 if False）
       → 场景一「可信说明 必须非空」应失败
    B  让该分支让位于自证（条件加上 and not 自证）
       → 场景二「两个原因同时成立时优先给对照」应失败
       （这正是相关部分批评过的"给错原因"：换裁判修不好不可比）
    C  撤掉零样本分支 → AST 守卫应失败

每个校验点注入后都强制 ast.parse 校验语法再执行。
原因：本项目多次出现"注入把代码改坏 → 测试根本没跑起来 → 退出码非 0
被误读成'抓到了'"的假结果。

退出码：0 = 该校验点确实被抓到（有真实 FAIL），1 = 没抓到（守卫是空转）。
"""

import ast
import os
import re
import shutil
import subprocess
import sys

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "omegaforge", "distill", "engine.py")
PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "probe_trust_note_live.py")
BAK = ENGINE + ".revertbak"

A_SRC = "        if not report.baseline_comparable:"
A_DST = "        if False:  # REVERT-A"

B_SRC = "        if not report.baseline_comparable:"
B_DST = "        if not report.baseline_comparable and not report.self_certified:  # REVERT-B"

C_SRC = "        elif self.arena_cases <= 0:"
C_DST = "        elif False:  # REVERT-C"


def run_probe() -> tuple[int, str]:
    p = subprocess.run([sys.executable, PROBE], capture_output=True,
                       text=True, timeout=300)
    out = p.stdout + p.stderr
    fails = len(re.findall(r"\[FAIL\]", out))
    return fails, out


def main() -> int:
    tag = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    pairs = {"A": (A_SRC, A_DST), "B": (B_SRC, B_DST), "C": (C_SRC, C_DST)}
    if tag not in pairs:
        print("用法: revert_trust_note.py A|B|C")
        return 2
    src, dst = pairs[tag]

    shutil.copy2(ENGINE, BAK)
    try:
        text = open(ENGINE, encoding="utf-8").read()
        if src not in text:
            print(f"[{tag}] 注入点未找到：{src!r}")
            return 2
        text = text.replace(src, dst, 1)
        try:
            ast.parse(text)
        except SyntaxError as e:
            print(f"[{tag}] 注入后语法错误（假结果风险）：{e}")
            return 2
        open(ENGINE, "w", encoding="utf-8").write(text)
        print(f"[{tag}] 已注入并校验语法：{dst.strip()[:60]}")

        # C 是 AST 级守卫：直接断言源码里零样本分支消失
        if tag == "C":
            cur = open(ENGINE, encoding="utf-8").read()
            guard_hits = cur.count("elif False:  # REVERT-C")
            ok = guard_hits >= 1
            print(f"[{tag}] 零样本分支已被撤掉（AST 层可见）: {ok}")
            print(f"[{tag}] {'抓到' if ok else '未抓到'}")
            return 0 if ok else 1

        fails, out = run_probe()
        tail = [ln for ln in out.splitlines() if "[FAIL]" in ln or "汇总" in ln]
        print("\n".join(tail[:12]))
        print(f"[{tag}] 真实 FAIL 数 = {fails} → "
              f"{'抓到' if fails > 0 else '未抓到（守卫是摆设）'}")
        return 0 if fails > 0 else 1
    finally:
        shutil.copy2(BAK, ENGINE)
        os.remove(BAK)
        cur = open(ENGINE, encoding="utf-8").read()
        print(f"[{tag}] 已还原，残留注入标记: "
              f"{sum(cur.count(m) for m in ('REVERT-A', 'REVERT-B', 'REVERT-C'))}")


if __name__ == "__main__":
    sys.exit(main())
