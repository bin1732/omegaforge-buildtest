#!/usr/bin/env python3
"""反向校验点的有效性盘点：注入必须真的造成故障。

反向校验点（``reverse=True``）用来证明判定助手没被环境故障骗过：注入一个
与守卫无关的故障，任何点名都不该被满足，判定必须给出"未抓到"。

它成立的前提是**注入真的造成了故障**。注入落在注释或文档字符串里时，那行
只是文本、不是代码，用例照常全绿——判定助手同样给出"未抓到"，于是校验点
恒真：每次都"通过"，但从未验到任何东西。这种假校验点与真校验点在"通过"
这件事上完全一样，只能靠"注入后有没有故障"区分。

判定方式：逐个执行反向锚点，读判定理由。

 · 理由含"用例全绿"或"注入未造成任何故障" → 注入没生效，校验点是假的
 · 理由含收集报错、失败项对不上号、整片都红 → 注入生效，判定助手识破了它

用法::

    python3 probes/audit_reverse_anchors.py              # 只列清单
    python3 probes/audit_reverse_anchors.py --run        # 逐个执行验证
    python3 probes/audit_reverse_anchors.py --run X      # 只跑标识为 X 的
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANCHOR_VARS = ("ANCHORS", "CASES", "POINTS", "CHECKS")

# 本脚本要串行地拉起多个回退脚本，因此自己先占住那把锁，再让子进程不去占。
# 顺序反过来的话：子进程各自占锁，而上一子进程派生的 pytest 若仍存活，
# 它会继承锁的文件描述符并继续持有——下一子进程于是全部报"锁被占用"，
# 五个校验点都判成"未能判定"。这类失败看着像锁有问题，实际是孙进程滞留。
os.environ["REV_NO_LOCK"] = "1"
sys.path.insert(0, str(ROOT))
from probes import _rev_verdict as _rv  # noqa: E402


def claim() -> None:
    _rv._claim_lock()

# 注入生效时判定助手给出的理由；命中即说明注入真的造成了故障
_LIVE = ("rc=", "有失败但不是预期", "整片都红", "预期之外还失败", "没有失败用例")
# 注入未生效时的理由
_DEAD = ("用例全绿", "注入未造成任何故障")


def find_reverse(script: Path) -> list[str]:
    """取出脚本里所有反向校验点的标识。

    锚点是形如 (标识, 说明, 注入表, 预期清单, 是否反向) 的元组，末位为
    True 即反向。用语法树取而不用文本匹配：注入表里可能含任意字符。
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        # 带类型注解的赋值走 AnnAssign（ANCHORS: list[...] = [...]），
        # 与裸赋值（Assign）是两类节点，只认后者会漏掉整批脚本。
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not isinstance(node.value, ast.List):
            continue
        if not any(isinstance(t, ast.Name) and t.id in ANCHOR_VARS
                   for t in targets):
            continue
        for el in node.value.elts:
            if not isinstance(el, ast.Tuple) or not el.elts:
                continue
            first = el.elts[0]
            if not isinstance(first, ast.Constant):
                continue
            ident = str(first.value)
            last = el.elts[-1]
            flagged = isinstance(last, ast.Constant) and last.value is True
            # 反向的标记方式不止一种：有的脚本末位写 True，有的不在锚点里
            # 标记、而是在循环里按标识判断（`if name == "X": ok = not got`）。
            # 只认前一种会漏掉整批脚本，而漏掉的表现是"清单里没有它"——
            # 与"这个脚本没有反向校验点"在输出上一样。
            if flagged or ident == "X":
                out.append(ident)
    return out


def scripts_with_anchors() -> list[Path]:
    seen = []
    for d in (ROOT / "probes", ROOT / "scripts"):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            n = p.name
            if n.startswith(("rev_", "revert_")) and n != Path(__file__).name:
                seen.append(p)
    return seen


def run_one(script: Path, ident: str) -> tuple[str, str]:
    """执行单个反向锚点，返回（结论, 理由）。"""
    # 每个子进程开跑前先自愈：上一个被中断时源码会停在注入态，下一个
    # 校验点于是在脏代码上跑，症状表现为"点名对不上"。
    healed = _rv.self_heal()
    if healed:
        pass
    env = dict(os.environ, REV_NO_LOCK="1")
    try:
        p = subprocess.run([sys.executable, str(script), ident],
                           cwd=str(ROOT), capture_output=True, text=True,
                           timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return "未知", "执行超时"
    txt = (p.stdout or "") + (p.stderr or "")
    reason = ""
    m = re.search(r"\[(抓到|未抓到)\]\s+\S+\s+[^:]*:\s*(.*)", txt)
    if m:
        reason = m.group(2).strip()
    if any(k in txt for k in _DEAD) or any(k in reason for k in _DEAD):
        return "假校验点", reason or "未取到判定理由"
    if any(k in reason for k in _LIVE) or any(k in txt for k in _LIVE):
        return "有效", reason
    return "未知", reason or txt.strip().splitlines()[-1][:120]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    do_run = "--run" in args
    only = [a for a in args if a != "--run"]

    table = []
    for s in scripts_with_anchors():
        idents = find_reverse(s)
        if idents:
            table.append((s, idents))

    total = sum(len(i) for _, i in table)
    print(f"含反向校验点的脚本 {len(table)} 个，校验点 {total} 个")
    if not do_run:
        for s, idents in table:
            print(f"  {s.relative_to(ROOT)}: {', '.join(idents)}")
        print("\n（加 --run 逐个执行验证注入是否真的造成故障）")
        return 0

    bad = 0
    unknown = 0
    ran = 0
    claim()
    for s, idents in table:
        for ident in idents:
            if only and ident not in only:
                continue
            ran += 1
            verdict, reason = run_one(s, ident)
            flag = {"有效": "[有效]", "假校验点": "[假]", "未知": "[?]"}[verdict]
            print(f"  {flag} {s.name} {ident}: {reason[:90]}")
            bad += int(verdict == "假校验点")
            unknown += int(verdict == "未知")
    print(f"\n执行 {ran} 个：有效 {ran - bad - unknown}，假校验点 {bad}，"
          f"未能判定 {unknown}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
