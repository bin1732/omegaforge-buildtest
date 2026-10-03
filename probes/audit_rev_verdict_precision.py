#!/usr/bin/env python3
"""盘点回退校验脚本自身的判定精度。

回退校验用来证明"守卫真的抓得住问题"，但它自己也需要被验。三种形态：

 · 点名（精确）：判定落到具体哪几条用例失败。收集阶段报错、失败的是别的
   用例，都判不通过。
 · 数条数（半精确）：只看失败了几条。比只看退出码进了一步，但仍说明不了
   失败的是不是预期那一条——注入把模块写坏时条数同样够。
 · 看退出码（粗糙）：rc 非零即记抓到。任何注入都能"抓到"。

识别靠静态扫描，而点名的写法有多种（共用判定助手、比对失败用例标识、比对
[FAIL] 行名），漏掉任何一种都会把"已点名"误报成"粗糙"。误报比漏报危险——
它会引着人去改本来没问题的脚本。

因此本脚本另设自检入口（--selftest）：构造三种形态的样本，确认各自被判成
对应形态。识别规则一旦写偏，自检会先红。

用法：
    python3 probes/audit_rev_verdict_precision.py
    python3 probes/audit_rev_verdict_precision.py --selftest
    python3 probes/audit_rev_verdict_precision.py --dir /tmp/样本目录
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBES = ROOT / "probes"

# 点名：判定落到具体的用例标识
NAMED_PAT = re.compile(
    r"_rev_verdict|verdict\(|"          # 共用判定助手
    r"expect_ids|"                       # 比对失败用例编号
    r"in failed\b|not in failed\b|"      # 比对失败标识集合
    r"FAILED|\[FAIL\]|"                  # 从输出里取失败用例名再比对
    # 不跑 pytest 的校验：直接调用扫描器，按命中条目的归属过滤
    r"for f in fails if|"
    r"basename\(.*\) in f")

# 数条数：以失败条数是否满足下限为准
COUNT_PAT = re.compile(
    r"n >= expect|n >= |nfail >= |"
    r"\((\d+) failed\)|(\d+) failed\"\)|failed_count|失败/错误|"
    r"ok = n > 0|n > 0|nfail > 0")

# 看输出：以审计输出里有没有某句判定为准。比看退出码精确——审计整体崩掉时
# 输出里没有那句话，会被判未抓到，而不是"任何注入都抓到"。
OUTPUT_PAT = re.compile(
    r'if\s+"[^"]+"\s+not\s+in\s+out'
    r'|"[^"]+"\s+not\s+in\s+out'
    r'|reason\s*=\s*"[^"]+"')

# 看退出码
RC_PAT = re.compile(r"caught = rc == 1|rc != 0 and|rc == 1\b|"
                    r"code != 0|returncode != 0")

MISSING_ANCHOR_PAT = re.compile(r"注入点不存在|锚点失效|不存在：|count\(old\)")

RECORDED_PAT = re.compile(
    r"results\[[^\]]+\] = False|"      # 记进结果字典
    r"total \+= 1|"                       # 靠 caught/total 比对收口
    r"\.append\(|\+= 1|\+=1|broken\b|nfail = -1|"
    r"return 1|ok = False|caught = False|all_ok = False|"
    r"raise (SystemExit|AssertionError)")


def classify(src: str) -> str:
    """判定形态：点名 / 数条数 / 看退出码。"""
    if NAMED_PAT.search(src):
        return "点名"
    if COUNT_PAT.search(src):
        return "数条数"
    if OUTPUT_PAT.search(src):
        return "看输出"
    if RC_PAT.search(src):
        return "看退出码"
    # 执行器类：不跑用例，验的是"注入/还原"这套机制本身（源码是否回到原样）
    if re.search(r"还原|git status|源码必须回到原样|残留", src):
        return "执行器类（不跑用例）"
    return "未识别"


def silent_on_missing_anchor(src: str) -> bool:
    """锚点失效时是否只打印一句就跳过。

    不能只看"全文有没有记载失败"：记载动作散落在别处（基线失败 return 1
    之类）时，一个纯 continue 的静默分支照样会被判成"有记载"。
    rev_orphan_reclaim 的锚点失效分支就是打印一句后 continue，且 hit 算了
    却从不累加，四个锚点全部没执行过仍返回 0——本脚本当时报"0 个静默"。

    因此就近判定：找到失效提示那一行，往下看几行内有没有真正把它记成
    失败；只有 continue / pass 则判静默。
    """
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if not MISSING_ANCHOR_PAT.search(ln):
            continue
        # 记载动作可能在提示之前（如 total += 1 计在循环开头），
        # 也可能在之后（如 bad.append）。前后各看两行。
        window = "\n".join(lines[max(0, i - 2):i + 5])
        if re.search(r"^\s*(continue|pass)\s*$", window, re.M):
            if not RECORDED_PAT.search(window):
                return True
    return False


def scan(d: Path) -> list:
    rows = []
    for f in sorted(d.glob("rev_*.py")):
        src = f.read_text(encoding="utf-8")
        rows.append((f.name, classify(src), silent_on_missing_anchor(src)))
    return rows


def report(rows: list) -> None:
    by = {}
    for _n, kind, _s in rows:
        by[kind] = by.get(kind, 0) + 1
    print(f"回退校验脚本 {len(rows)} 个")
    for kind in ("点名", "数条数", "看输出", "看退出码", "未识别"):
        if by.get(kind):
            print(f"  {kind}：{by[kind]} 个")
            for n, k, _s in rows:
                if k == kind:
                    print(f"      {n}")
    silent = [n for n, _k, s in rows if s]
    print(f"  锚点失效即静默跳过：{len(silent)} 个")
    for n in silent:
        print(f"      {n}")


def selftest() -> int:
    """自检：三种形态必须各自被判成对应形态。

    识别规则写偏的后果是误报，而误报会引着人去改本来没问题的脚本。这里用
    最小样本把三种形态钉住。
    """
    samples = {
        "rev_named.py": '''
from probes._rev_verdict import verdict
caught, why = verdict(rc, failed, expect)
for e in expect:
    if e not in failed:
        bad.append(e)
''',
        "rev_named2.py": '''
for line in out.splitlines():
    m = re.search(r"test_(\\d+)_", line)
    if m and "FAILED" in line:
        failed.add(int(m.group(1)))
hit = all(e in failed for e in expect_ids)
''',
        "rev_count.py": '''
n = failed_count(out)
caught = n >= expect and code != 0
''',
        "rev_count2.py": '''
n, tail = run_guard()
ok = n > 0
''',
        # 不跑 pytest 的校验：调用扫描器后按命中条目的归属过滤
        "rev_named3.py": '''
fails, _ = m.scan(Path(ROOT))
hit = [f for f in fails if os.path.basename(path) in f[1]]
check(tag, len(hit) >= 1, "命中")
''',
        "rev_rc.py": '''
caught = rc == 1
''',
        # 以输出里有没有那句判定为准 —— 判"看输出"
        "rev_output.py": '''
if code == 0:
    return "审计仍判通过"
if "接口应答正常" not in out:
    return f"不是因为应答判定：{out[-200:]!r}"
return None
''',
        # 锚点失效后只 continue —— 判静默
        "rev_silent.py": '''
if old not in orig:
    print(f"[{name}] 注入点不存在 —— 锚点失效")
    continue
''',
        # 锚点失效后明确记为未抓到 —— 不判静默
        "rev_recorded.py": '''
if old not in orig:
    print(f"[{name}] 锚点失效（记为未抓到）")
    bad.append(name)
    continue
''',
    }
    want = {"rev_named.py": "点名", "rev_named2.py": "点名",
            "rev_named3.py": "点名",
            "rev_count.py": "数条数", "rev_count2.py": "数条数",
            "rev_rc.py": "看退出码", "rev_output.py": "看输出"}
    bad = []
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name, body in samples.items():
            (d / name).write_text(body, encoding="utf-8")
        got = {n: k for n, k, _s in scan(d)}
        silent = {n: sv for n, _k, sv in scan(d)}
    want_silent = {"rev_silent.py": True, "rev_recorded.py": False}
    for name, want_s in want_silent.items():
        got_s = silent.get(name)
        okk = got_s == want_s
        print(f"  [{'抓到' if okk else '未抓到'}] {name} 静默={want_s} "
              f"实际={got_s}")
        if not okk:
            bad.append(name)
    for name, exp in want.items():
        okk = got.get(name) == exp
        print(f"  [{'抓到' if okk else '未抓到'}] {name} 期望={exp} 实际={got.get(name)}")
        if not okk:
            bad.append(name)
    total = len(want) + len(want_silent)
    print(f"\n自检：{total - len(bad)}/{total} 项识别正确")
    return 1 if bad else 0


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    d = PROBES
    if "--dir" in args:
        d = Path(args[args.index("--dir") + 1])
    rows = scan(d)
    report(rows)
    print("\n注一：粗糙不等于一定失效，只是没有证据表明它抓的是预期的那一条。")
    print("注二：本脚本是静态扫描，区分不了\"代码里真判定\"与\"说明里提到\"——")
    print("      一个只看退出码的脚本，只要说明里写了 FAILED 就会被判成")
    print("      点名。因此\"点名\"一栏只表示看得出点名写法，最终仍需人工确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
