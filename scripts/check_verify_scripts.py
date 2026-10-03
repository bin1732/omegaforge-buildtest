#!/usr/bin/env python3
"""验收脚本必须能被导入，且它引用的成员必须真实存在。

## 为什么要单独查

CI 上的验收脚本只在 Windows 装机实例上执行，本地跑不到。若某个脚本引用了
一个不存在的成员（例如 `from install_layout_checks import find_sidecar`，
而该模块从未定义过这个名字），本地所有检查都绿——因为本地根本不执行它。
它只会在 runner 上一上来就 ImportError 崩掉，表现为"某个验收步骤失败"，
而报错指向的是验收脚本本身，排查方向会被带到产品问题上去。

历史上真实发生过一次：卸载验收因此从未真正跑过一轮，却一直被记为"已接进
流程"。

## 判定口径

1. 脚本集不能为空：glob 取不到任何脚本时必须判失败。空集通过等于这个检查
   恒真——查了零个脚本却报"全部正常"。
2. 逐个在独立子进程里导入：导入期副作用不互相污染，且超时可控。子进程返回
   码非 0 即失败，并回传尾部输出——只回传"失败"两个字的诊断等于没有诊断。
3. 静态查 `from 同级模块 import 成员`：成员必须在该模块的顶层名字集合里。
   这层查的是"导入期就会炸"的引用，不依赖执行到那一行。

## 不做的事

不检查这些脚本的运行结果——那由 CI 上的真实执行负责。这里只保证"能起跑"。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

# 每个脚本的导入超时（秒）。取 30 是为了容纳解释器冷启动与磁盘抖动；超时
# 判失败而不是判成功——安静地等很久，比直接失败更难查。
IMPORT_TIMEOUT = 30

# 发现不到脚本时的下限。低于此数说明 glob 或工作目录不对，此时"全部通过"
# 是假的。
MIN_SCRIPTS = 8

PATTERNS = ("ci_*.py", "verify_*.py")


def discover(scripts_dir: Path) -> list[Path]:
    """取出需要保证可起跑的脚本（CI 步骤脚本与验收脚本）。"""
    out: list[Path] = []
    for pat in PATTERNS:
        out.extend(sorted(scripts_dir.glob(pat)))
    return sorted(set(out))


def defined_names(path: Path) -> set[str]:
    """模块顶层定义的名字（函数、类、赋值、带注解赋值）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def missing_members(scripts_dir: Path) -> list[str]:
    """`from 同级脚本 import 成员` 里不存在的成员。

    名字表必须取 scripts 目录下的**全部** .py，不能只取被检查的那几个：
    真实失效恰恰发生在被 import 的辅助模块上（install_layout_checks.py
    既不匹配 ci_* 也不匹配 verify_*），只取被检查项会让那类引用整个漏掉。
    """
    scripts = discover(scripts_dir)
    defined = {p.stem: defined_names(p) for p in scripts_dir.glob("*.py")}
    problems: list[str] = []
    for p in scripts:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if mod not in defined:
                continue  # 第三方或标准库，由导入检查负责
            known = defined[mod]
            for alias in node.names:
                if alias.name != "*" and alias.name not in known:
                    problems.append(
                        f"{p.name}:{node.lineno}: from {mod} import {alias.name}"
                        f"（{mod}.py 未定义该名字）"
                    )
    return problems


def import_failures(scripts_dir: Path) -> list[str]:
    """逐个在子进程里导入，返回不可导入的脚本及原因。"""
    problems: list[str] = []
    for p in discover(scripts_dir):
        try:
            r = subprocess.run(
                [sys.executable, "-c", f"import {p.stem}"],
                cwd=str(scripts_dir), capture_output=True, text=True,
                timeout=IMPORT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"{p.name}: 导入超时（{IMPORT_TIMEOUT}s）")
            continue
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()
            last = tail[-1] if tail else "(无 stderr)"
            problems.append(f"{p.name}: 导入失败 rc={r.returncode} — {last}")
    return problems


def check(scripts_dir: Path) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    scripts = discover(scripts_dir)
    if len(scripts) < MIN_SCRIPTS:
        return [
            f"只发现 {len(scripts)} 个脚本（下限 {MIN_SCRIPTS}）："
            f"glob 或工作目录不对时，空集会伪装成全部通过"
        ]
    problems = missing_members(scripts_dir) + import_failures(scripts_dir)
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    scripts_dir = root / "scripts"
    problems = check(scripts_dir)
    if problems:
        print(f"验收脚本起跑检查失败（{len(problems)} 项）：")
        for item in problems:
            print("  -", item)
        return 1
    print(f"验收脚本起跑检查通过 ✓（{len(discover(scripts_dir))} 个脚本均可导入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
