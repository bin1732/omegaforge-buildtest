#!/usr/bin/env python3
"""守卫覆盖盘点（用例级）：每条用例分别有多少回退证据。

## 为什么需要

按文件判定覆盖会把"只点名了其中几条用例"的文件算作整体覆盖。文件级
覆盖率因此系统性偏高，而偏高部分正是没有回退证据的那部分用例——一个
文件里没被任何锚点点名的用例，没有任何脚本能证明它有效。

本脚本把口径下沉到用例。对每个用例文件分别给出：

  · 全量用例数（参数化按基名归并，同一条的不同实例算一条）
  · 被回退脚本点名的用例数（有独立回退证据）
  · 未被点名的用例名（没有独立回退证据）

## 判定口径

点名 = 回退脚本里能折叠出 `tests/<文件>.py::<用例>` 形式的字符串。折叠
只处理常量相加、f-string 与单层变量引用，折叠不了的形式不计入，因此
用例级覆盖率是**下界**。

"未被点名"的含义是：没有任何锚点能在注入后让该用例变红，即它没有独立
的回退证据。这不等于该用例无效——它可能与其他用例共享同一性质、由同一
个锚点连同覆盖。

用法：

    python3 probes/audit_guard_case_coverage.py             # 盘点
    python3 probes/audit_guard_case_coverage.py --selftest  # 自检
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL_RE = re.compile(
    r"^(?:tests/)?([A-Za-z0-9_]+\.py)::(?:[A-Za-z0-9_]+::)?(test_[A-Za-z0-9_]+)$")
CLASS_RE = re.compile(
    r"^(?:tests/)?([A-Za-z0-9_]+\.py)::([A-Za-z0-9_]+)$")
CASE_FILE_RE = re.compile(r"(?:tests/)?(test_\w+\.py)")


def _case_helpers(tree, env):
    """模块内 case 式辅助函数的（参数名 -> 返回模板）映射。

    不少回退脚本把拼接写成 `case("test_x")`，靠一个辅助函数补上文件与
    类名前缀。折叠不了函数调用的话，这类脚本整体提取不到——而提取不到
    与"这个文件没有用例级证据"在输出上完全一样，会把本来有证据的文件
    算进缺口，把工作量引向已经做过验证的地方。
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in ("case", "_case", "c", "tc", "cs"):
            continue
        body = [b for b in node.body if not isinstance(b, ast.Pass)]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        if not isinstance(body[0].value, (ast.JoinedStr, ast.BinOp,
                                           ast.Constant)):
            continue
        args = [a.arg for a in node.args.args]
        if not args:
            continue
        out[node.name] = (args, body[0].value, env)
    return out


def _fold(node, env, helpers=None):
    """把字符串拼接节点折叠成常量；折叠不了返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if helpers and isinstance(node, ast.Call) \
            and isinstance(node.func, ast.Name) and node.func.id in helpers:
        args, tmpl, base_env = helpers[node.func.id]
        if len(node.args) == len(args):
            local = dict(base_env)
            for k, a in zip(args, node.args):
                v = _fold(a, env, helpers)
                if v is None:
                    return None
                local[k] = v
            return _fold(tmpl, local, helpers)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold(node.left, env)
        right = _fold(node.right, env)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                s = _fold(v.value, env)
                if s is None:
                    return None
                parts.append(s)
            else:
                return None
        return "".join(parts)
    return None


def _norm_file(base: str) -> str:
    return "tests/" + base


def _var_env(tree, src: str) -> dict:
    """变量到字符串的映射。

    折叠不了的形式（如 pathlib 拼接）退一步取字面量里的用例文件名，否则
    凡用 `ROOT / "tests" / "test_x.py"` 定位的脚本会整体提取不到。
    """
    env = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            val = _fold(node.value, env)
            if val is not None:
                env[node.targets[0].id] = val
                continue
            seg = ast.get_source_segment(src, node.value) or ""
            m = CASE_FILE_RE.search(seg)
            if m:
                env[node.targets[0].id] = _norm_file(m.group(1))
    return env


def extract_pairs(src: str) -> set:
    """从一份回退脚本源码里提取 (用例文件, 用例名) 集合。"""
    tree = ast.parse(src)
    env = _var_env(tree, src)
    helpers = _case_helpers(tree, env)

    pairs = set()
    for node in ast.walk(tree):
        val = _fold(node, env, helpers)
        if not val or len(val) > 300 or "::" not in val:
            continue
        m = FULL_RE.match(val)
        if m:
            pairs.add((_norm_file(m.group(1)), m.group(2)))
            continue
        if ".py::" in val:
            f, rest = val.split(".py::", 1)
            seg = rest.split("::")[-1]
            if seg.startswith("test_"):
                pairs.add((_norm_file(f.split("/")[-1]), seg))
    return pairs


def extract_class_targets(src: str) -> set:
    """类级或文件级的运行目标——跑了该文件，但未点名具体用例。"""
    tree = ast.parse(src)
    env = _var_env(tree, src)
    helpers = _case_helpers(tree, env)
    out = set()
    for node in ast.walk(tree):
        val = _fold(node, env, helpers)
        if not val or len(val) > 300 or "::" not in val:
            continue
        m = CLASS_RE.match(val)
        if m:
            out.add(_norm_file(m.group(1)))
    return out


def rev_scripts() -> list:
    names = set()
    for d in (ROOT / "probes", ROOT / "scripts"):
        for p in d.glob("rev*.py"):
            names.add(p)
        for p in d.glob("revert_*.py"):
            names.add(p)
    return sorted(names)


def collect_cases() -> dict:
    """每个用例文件收录多少条用例（参数化按基名归并）。"""
    sys.path.insert(0, str(ROOT))
    from probes._pytest_env import env
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--collect-only",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, env=env(), timeout=600)
    per = {}
    for line in p.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        f = line.split("::")[0]
        base = line.split("::")[-1].split("[")[0]
        per.setdefault(f, set()).add(base)
    return per


def selftest() -> int:
    """自检：可折叠的形态必须提取到，不可折叠的不得计入。"""
    samples = [
        ("常量相加", 'C = "tests/a.py" + "::"\nX = [C + "test_one"]\n',
         {("tests/a.py", "test_one")}, None),
        ("f-string", 'G = "tests/b.py"\nX = [f"{G}::test_two"]\n',
         {("tests/b.py", "test_two")}, None),
        ("两跳引用", 'G = "tests/c.py"\nC = G + "::"\nX = [C + "test_three"]\n',
         {("tests/c.py", "test_three")}, None),
        ("路径拼接", 'TEST = ROOT / "tests" / "test_p.py"\n'
                     'P = TEST + "::"\nX = [P + "test_four"]\n',
         {("tests/test_p.py", "test_four")}, None),
        ("类级目标", 'X = "test_q.py::TestFoo"\n', set(), {"tests/test_q.py"}),
        ("运行时取值", 'C = "tests/d.py::"\nX = [C + pick()]\n', set(), None),
        ("缺分隔符", 'C = "tests/e.py"\nX = [C + "test_five"]\n', set(), None),
        ("case 辅助函数",
         'T = "tests/f.py"\ndef case(n):\n    return f"{T}::{n}"\n'
         'X = [case("test_six")]\n',
         {("tests/f.py", "test_six")}, None),
        ("case 带类名",
         'T = "tests/g.py"\ndef case(k, n):\n    return f"{T}::{k}::{n}"\n'
         'X = [case("TestH", "test_seven")]\n',
         {("tests/g.py", "test_seven")}, None),
    ]
    bad = 0
    for name, src, want, want_files in samples:
        got = extract_pairs(src)
        ok = got == want
        if want_files is not None:
            ok = ok and extract_class_targets(src) == want_files
        bad += 0 if ok else 1
        print(f"  [{'抓到' if ok else '未抓到'}] {name:<10} "
              f"期望={sorted(want)} 实际={sorted(got)}")
    print(f"自检：{len(samples) - bad}/{len(samples)} 形态识别正确")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    cases = collect_cases()
    named = {}
    file_level = set()
    for s in rev_scripts():
        try:
            src = s.read_text(encoding="utf-8")
        except OSError:
            continue
        for f, case in extract_pairs(src):
            named.setdefault(f, set()).add(case)
        file_level |= extract_class_targets(src)

    total = sum(len(v) for v in cases.values())
    proved = sum(len(v & cases.get(f, set())) for f, v in named.items())
    files_total = len([f for f in cases if f.startswith("tests/")])
    files_hit = len([f for f in named if f in cases])

    print(f"全量用例（参数化按基名归并）：{total} 条，分布在 {files_total} 个文件")
    print(f"有独立回退证据：{proved} 条")
    print(f"用例级覆盖率：{proved / max(1, total):.0%}")
    print(f"文件级覆盖率（对照口径）：{files_hit / max(1, files_total):.0%}")
    only_file = sorted(f for f in file_level if f in cases and f not in named)
    if only_file:
        print(f"\n只有类级/文件级运行目标、没有用例级点名的文件 {len(only_file)} 个：")
        for f in only_file[:10]:
            print(f"  {len(cases[f]):>4}  {f}")
        if len(only_file) > 10:
            print(f"  …… 另有 {len(only_file) - 10} 个")

    rows = []
    for f, ns in named.items():
        allc = cases.get(f, set())
        missing = sorted(allc - ns)
        if missing:
            rows.append((len(missing), f, len(ns & allc), len(allc)))
    rows.sort(reverse=True)
    print("\n已覆盖文件中，没有独立回退证据的用例（缺口多的在前）：")
    for miss, f, n, tot in rows[:args.top]:
        print(f"  {miss:>4}  {f:<48} 点名 {n:>2} / {tot:>2}")
    if len(rows) > args.top:
        print(f"  …… 另有 {len(rows) - args.top} 个文件存在缺口")
    print("\n注：点名靠静态折叠提取，折叠不了的形式不计入，故用例级覆盖率"
          "是下界；未被点名表示没有独立回退证据，不等于该用例无效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
