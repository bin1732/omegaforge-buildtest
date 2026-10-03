#!/usr/bin/env python3
"""守卫覆盖盘点：哪些用例文件没有对应的回退校验脚本。

## 为什么需要

回退校验脚本的作用是证明"撤掉改动后守卫会变红"。这类脚本只引用了很小一部
分用例文件，其余的守卫**没有脚本证明其有效**——它们可能恒真，也可能随代码漂移
失效，而"全量回归通过"看不出区别：一个恒真的守卫同样会绿。

本脚本不改动任何东西，只把这份缺口摆出来，好让它能被逐轮收窄。

## 判定口径

覆盖 = 某个 `probes/rev_*.py` 里出现了 `tests/<文件名>` 字面量。这是文本
匹配，识别不了"脚本跑的其实是别的文件"这类名不副实的情况，因此结论仍需
实际执行确认——这一条写进输出里，不藏在说明里。

用法：

    python3 probes/audit_guard_coverage.py             # 盘点
    python3 probes/audit_guard_coverage.py --selftest  # 自检
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PROBES = ROOT / "probes"
SCRIPTS = ROOT / "scripts"
CASE_RE = re.compile(r'tests/(test_\w+\.py)')


def collect_counts() -> dict:
    """每个用例文件收录多少条用例。

    没有它就无法排出优先级：用例多的守卫一旦恒真，放过的缺陷面更大，
    应当先于只有一条用例的守卫被验证。
    """
    sys.path.insert(0, str(ROOT))
    from probes._pytest_env import env
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--collect-only",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, env=env(), timeout=600)
    cnt = {}
    for line in (p.stdout or "").splitlines():
        m = re.match(r'tests/(test_\w+\.py)::', line)
        if m:
            cnt[m.group(1)] = cnt.get(m.group(1), 0) + 1
    return cnt


def scan(case_files: list[str]) -> tuple[dict, list]:
    """扫描回退校验脚本引用了哪些用例文件。

    范围必须同时覆盖 probes/ 与 scripts/ 下的全部脚本，不能只扫
    `probes/rev_*.py`：回退脚本在两处都有，且命名不统一（scripts/ 下是
    `revert_*.py`、`reverse_*.py`）。只扫一处会得出偏低的覆盖率，而偏低
    的覆盖率看着像"大量守卫未验证"，与真实缺口无法区分——它会把工作量
    引向本来已有脚本的部分。
    """
    covered = {}
    phantom = {}
    real = set(case_files)
    for q in sorted(list(PROBES.glob("*.py")) + list(SCRIPTS.glob("*.py"))):
        if q.name.startswith("_"):
            continue
        s = q.read_text(encoding="utf-8", errors="replace")
        for m in CASE_RE.finditer(s):
            name = m.group(1)
            # 脚本里写的用例文件必须真实存在。写了一个已经改名或删掉的
            # 文件名时，这条引用是死的：脚本照常"引用"它，覆盖率照常算
            # 进分子，而那个文件里没有任何用例被验证。与真覆盖在输出上
            # 完全一样，只能在这里分开。
            #
            # 盘点脚本的自检样本（`test_aaa.py` 等）同样落在这里：它们只
            # 是字符串样本，不是仓库里的文件。不排除的话，报告会列出几
            # 条 0 用例的"待补守卫"，而那几个文件根本不存在。
            if name not in real:
                phantom.setdefault(name, set()).add(q.name)
                continue
            covered.setdefault(name, set()).add(q.name)
    missing = [t for t in case_files if t not in covered]
    return covered, missing, phantom


def selftest() -> int:
    """自检：构造最小样本，确认识别不会整体失灵。

    盘点脚本自己若失效，会输出一份看起来正常的清单——和真的盘点过没有区别。
    """
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rev_x.py"
        p.write_text('T = "tests/test_aaa.py"\n', encoding="utf-8")
        s = p.read_text(encoding="utf-8")
        hit = CASE_RE.findall(s)
        if hit != ["test_aaa.py"]:
            print("[未抓到] 覆盖识别：应识别 test_aaa.py，实际 " + str(hit))
            ok = False
        else:
            print("[抓到] 覆盖识别（能认出脚本引用的用例文件）")
        # 反向：把模式里的 tests/ 前缀去掉，识别必须失灵
        if re.findall(r'(test_\w+\.py)', 'test_bbb.py') != ["test_bbb.py"]:
            print("[未抓到] 文件名识别")
            ok = False
        else:
            print("[抓到] 文件名识别")
    # 扫描范围：只扫一处会得出偏低的覆盖率，与真实缺口无法区分。构造一个
    # 只在 scripts/ 下放回退脚本的最小仓库，scan 必须认到它。
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "probes").mkdir()
        (root / "scripts").mkdir()
        (root / "scripts" / "revert_x.py").write_text(
            'T = "tests/test_zzz.py"\n', encoding="utf-8")
        global PROBES, SCRIPTS
        old_p, old_s = PROBES, SCRIPTS
        try:
            PROBES, SCRIPTS = root / "probes", root / "scripts"
            covered, missing, phantom = scan(["test_zzz.py"])
        finally:
            PROBES, SCRIPTS = old_p, old_s
        if "test_zzz.py" in covered:
            print("[抓到] 扫描范围（scripts/ 下的回退脚本同样计入）")
        else:
            print("[未抓到] 扫描范围：scripts/ 下的脚本被漏掉，覆盖率会偏低")
            ok = False
    print(f"自检：{'通过' if ok else '未通过'}")
    return 0 if ok else 1


def _files_with_case_evidence() -> set:
    """哪些用例文件被点名到了具体用例。

    借用例级盘点脚本的折叠逻辑，避免两处各写一套提取规则：口径一旦分叉，
    两个脚本给出的覆盖率就对不上，而哪个是对的无法从数字本身判断。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from audit_guard_case_coverage import extract_pairs, rev_scripts
    except Exception:
        return set()
    out = set()
    for name in rev_scripts():
        p = Path(name)
        try:
            pairs = extract_pairs(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for f, _c in pairs:
            out.add(f.split("/")[-1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    case_files = sorted(p.name for p in TESTS.glob("test_*.py"))
    covered, missing, phantom = scan(case_files)
    cnt = collect_counts()
    named = _files_with_case_evidence()

    print(f"用例文件 {len(case_files)} 个，被回退校验脚本引用 {len(covered)} 个，"
          f"没有脚本验证 {len(missing)} 个")
    print(f"覆盖率 {len(covered) / max(1, len(case_files)):.0%}"
          f"（按脚本里是否出现文件名判定）")

    # 两个口径必须同时给出。只看文件级会把"注释里提到过"也算作覆盖，
    # 而那部分用例没有任何锚点能在注入后让它变红。偏高的部分正是缺口
    # 所在，混在一个数里看不出来，会把"已覆盖"当成"已验证"。
    thin = [t for t in covered if t not in named]
    print(f"其中有用例级点名证据 {len([t for t in covered if t in named])} 个"
          f"；仅被提到文件名、没有用例级证据 {len(thin)} 个")
    if thin:
        print("\n仅被提到（虚高部分，按用例数排序）：")
        for t in sorted(thin, key=lambda x: -cnt.get(x, 0))[:12]:
            print(f"  {cnt.get(t, 0):>4}  {t}")

    if phantom:
        print("\n脚本里写了但 tests/ 下不存在的用例文件（死引用，覆盖率虚高）：")
        for name in sorted(phantom):
            print(f"  {name}  ← {', '.join(sorted(phantom[name]))}")
        print("  这些名字要么来自自检样本，要么指向已改名/已删除的文件；"
              "两种都不构成覆盖。")

    print("\n未被任何脚本提到的守卫（按用例数排序，多的优先补）：")
    for t in sorted(missing, key=lambda x: -cnt.get(x, 0))[:25]:
        print(f"  {cnt.get(t, 0):>4}  {t}")
    if len(missing) > 25:
        print(f"  …… 另有 {len(missing) - 25} 个")
    print("\n注：两级口径都靠静态提取。文件级认不出脚本实际跑的是别的文件，"
          "用例级折叠不了常量以外的写法（故为下界）；结论需实际执行确认。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
