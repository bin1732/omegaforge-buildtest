#!/usr/bin/env python3
"""守卫说明里"引用了却不存在"的标识符盘点。

## 查的是什么

守卫文件的说明常常写"撤回修复必须变红（见 xxx）"。这类引用若指向一个
**仓库里并不存在**的名字，读到的人会以为有回退校验，实际没有——而它无法
被现有任何检查发现：说明文字整洁度扫描不管语义，守卫覆盖盘点只看有没有
脚本引用文件，都看不见"引用目标本身不存在"。

## 判定口径

只取**机制性引用**：以 `_` 开头、或全大写且含下划线的标识符。这两类几乎
必然是代码里的函数或常量，不会是数据字段（`judge_reasons`、`task_add` 这类
普通名字不参与判定，否则误报会淹没结论）。

判定"悬空"的依据是**出现次数**：该标识符在全仓库的出现次数若恰好等于它在
这句说明里的出现次数，说明除了这句引用之外别处一次都没出现。

按出现次数而不是按"是否有 def/class 定义"判定，是因为锚点也可能以注释块
形式存在（例如 `REVERT_ANCHORS` 是文件末尾的注释块，不是 Python 标识符）。
只认定义的话它会误报成悬空——启发式结论需实际确认，这一条已写进输出。

## 自检

`--selftest` 构造两个最小样本：一个引用仓库里真实存在的名字，一个引用
不存在的名字，脚本必须分别判"存在"与"悬空"。
"""
from __future__ import annotations

import argparse
import ast
import secrets
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".pylibs", "__pycache__", ".git", "node_modules", "dist",
             ".venv", "venv"}

# 机制性引用：下划线开头（内部函数/锚点），或全大写含下划线（常量/锚点块）
MECHANISM = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_mechanism(ident: str) -> bool:
    if not MECHANISM.match(ident):
        return False
    if ident.startswith("_"):
        return True
    return ident.isupper() and "_" in ident


def collect_refs(path: Path) -> list[str]:
    """从模块说明里提取机制性引用（反引号包裹者 + "见 X" 形式）。"""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for m in re.findall(r"`([A-Za-z_][A-Za-z0-9_]{2,})`", doc):
        if _is_mechanism(m):
            out.append(m)
    for m in re.findall(r"见\s*([A-Za-z_][A-Za-z0-9_]{3,})\s*(?:说明|）|\))", doc):
        if _is_mechanism(m):
            out.append(m)
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def count_many(idents: list[str]) -> dict:
    """一次遍历全仓，统计每个标识符的出现次数。

    逐个标识符各扫一遍全仓的话，文件数与标识符数相乘，扫描慢到会超时。
    """
    counts = {i: 0 for i in idents}
    for f in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if f.name.endswith(".bak"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i in idents:
            counts[i] += text.count(i)
    return counts


def count_in_doc(path: Path, ident: str) -> int:
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (SyntaxError, UnicodeDecodeError):
        return 0
    return doc.count(ident)


def selftest(tmp: Path) -> int:
    """两个最小样本：存在的判存在，不存在的判悬空。"""
    d = tmp / "st"
    d.mkdir(parents=True, exist_ok=True)
    real = d / "test_real.py"
    real.write_text('"""引用真实存在的名字（见 PYTEST_CURRENT_TEST）。"""\n',
                    encoding="utf-8")
    # 虚构名必须是运行时才生成的随机串：写死在脚本源码里的话，扫描会把
    # 脚本自己源码中的那几处也数进去，于是"不存在"被判成"存在"——自检
    # 自己去证自己，等于没证。
    ghost = "_NO_SUCH_ANCHOR_" + secrets.token_hex(4)
    fake = d / "test_fake.py"
    fake.write_text(f'"""引用不存在的名字（见 {ghost}）。"""\n',
                    encoding="utf-8")
    ok = True
    for f, ident, want in ((real, "count_everywhere", False),
                           (fake, ghost, True)):
        total = count_many([ident])[ident]
        dangling = total <= count_in_doc(f, ident)
        got = "悬空" if dangling else "存在"
        exp = "悬空" if want else "存在"
        flag = "抓到" if dangling == want else "未抓到"
        if dangling != want:
            ok = False
        print(f"  [{flag}] {f.name} {ident}：期望={exp} 实际={got}")
    print(f"自检：{'通过' if ok else '未通过'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tmp", default="/tmp")
    args = ap.parse_args()
    if args.selftest:
        return selftest(Path(args.tmp))

    per_file = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        refs = collect_refs(f)
        if refs:
            per_file.append((f, refs))
    idents = sorted({i for _, refs in per_file for i in refs})
    counts = count_many(idents) if idents else {}

    dangling = []
    checked = 0
    for f, refs in per_file:
        checked += 1
        for ident in refs:
            if counts.get(ident, 0) <= count_in_doc(f, ident):
                dangling.append((f.name, ident))

    print(f"含机制性引用的守卫 {checked} 个")
    print(f"引用不存在的标识符 {len(dangling)} 处")
    for name, ident in dangling:
        print(f"  悬空：{name} → `{ident}`")
    print("\n注：按全仓库出现次数判定，认得出以注释块形式存在的锚点；"
          "结论仍需实际确认被引用的机制是否真的可执行。")
    return 1 if dangling else 0


if __name__ == "__main__":
    sys.exit(main())
