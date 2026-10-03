#!/usr/bin/env python3
"""说明文字整洁度守卫：注释与文档字符串里不得出现内部痕迹。

## 为什么需要

代码里的说明文字会随源码一同发布。一旦混入开发期的作业记录
（"原先是 X"、"检验发现 Y"、"第 N 章"）或内部工具名，读者看到的
就是一本开发日志，而不是对约束的陈述。这类文字还会误导后来的人：
把已经解决的历史状态当成现存缺陷去修。

## 设计取舍（检验教训）

**误报比漏报危险。** 词表里若混入通用技术术语（"唯一真源""快照"
"漏掉"），会引着人去改本来没问题的表述。所以这里只收录确定属于
内部痕迹的词，并单独列出有正当理由的豁免项。

判定分两类：
- FAIL：确定是内部痕迹，命中即失败
- ALLOW：有正当理由且已书面说明，打印出来接受审视但不阻断

退出码 0 = 干净；1 = 有内部痕迹残留。
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

# Windows runner 检验：cp1252 下 print 中文会抛 UnicodeEncodeError。
# 这里强制 UTF-8，保证脚本单独运行也成立。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT_DEFAULT = "."

# 确定属于内部痕迹 —— 命中即失败
FAIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 注意："本版"不在此列——它在蒸馏语境下指"本次产出的这一版"，
    # 是业务概念而非作业记录，收进来会制造大量误报。
    ("过程叙事（描述过去状态）", re.compile(
        r"(此前|以前|原来(是|写作|这里)|早前|先前|旧版|修复前|修复后"
        # 「防修过头」是正当表述（意为"避免过度修复"），用后瞻排除，
        # 否则它会被"修过"命中——误报会引着人去改本来没问题的说明。
        r"|(?<!防)修过|已修|已确认|查证后|如实说明|本次修复"
        r"|上一轮|曾出现|一度引入)")),
    ("作业记录（试验与验证过程）", re.compile(
        r"(实测|验证结果|反向验证|联调实测|冒烟|探针|锚点"
        r"|互为掩护|假阳性|摆设|接线|守位)")),
    ("内部术语", re.compile(r"(守卫|回归|契约)")),
    # 斜杠式列举（"相关部分"）同样要认：只匹配单个数字的话，
    # 这种写法会整条漏掉。
    ("章节引用", re.compile(r"第\s*\d+(?:\s*[/、,，]\s*\d+)*\s*章")),
    ("构建与工具名", re.compile(r"(\bCI\b|\btsc\b|pytest|jsdom|全仓\s*grep)")),
    # 说明文字里的英文内部标识：面向使用者的表述中它们都有既定的中文说法，
    # 写英文等于把内部叫法带进说明。
    ("英文内部标识", re.compile(
        r"\b(naive|baseline_note|trust_note|claim_valid|self_certified"
        r"|exam_self_authored)\b", re.I)),
    # 独立的 prompt 指"提示词"这一概念；而 `payload.get(…)`、`payload.sh`
    # 这类是代码与文件名的直接引用，改写反而失真，故要求前者、放行后者。
    ("英文概念词", re.compile(r"(?<![`\w./-])(system\s+)?prompt(?![.\w(])")),
]

# 测试目录专用词表：只收录真正会误导读者的一类。
#
# 测试文件同样随仓库发布，说明文字要求一致。但"守卫""回归""契约""检查脚本"
# 在测试语境下是正当表述，收进来会制造大量误报——误报会引着人去改本来
# 没问题的表述，比漏报更危险。因此这里只查描述过去状态的作业记录。
TEST_FAIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("过程叙事（描述过去状态）", re.compile(
        r"(此前|以前|原来(是|写作|这里)|早前|先前|旧版|修复前|修复后"
        r"|(?<!防)修过|已修|已确认|查证后|如实说明|本次修复"
        r"|上一轮|曾出现|一度引入|至今未|从未被|尚未被)")),
    ("作业记录（试验与验证过程）", re.compile(
        r"(实测|验证结果|反向验证|联调实测|冒烟|互为掩护|假阳性|摆设)")),
    ("章节引用", re.compile(r"第\s*\d+(?:\s*[/、,，]\s*\d+)*\s*章")),
    # 英文内部标识：使用者侧对应中文说法，说明文字里写英文等于把内部叫法
    # 带进说明。测试文件同样随仓库发布，口径一致。
    ("英文内部标识", re.compile(
        r"\b(naive|baseline_note|trust_note|claim_valid|self_certified"
        r"|exam_self_authored)\b", re.I)),
    ("环境说明", re.compile(r"(依赖装不全|依赖未安装|缺少编译验证)")),
]

# 有正当理由、但必须被看见 —— 打印不阻断
# 理由：磁盘上的数据可能由更早的版本写入，容错必须考虑这种情况，
# 这是真实的技术约束，不是作业记录。
ALLOW_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("旧版本数据兼容", re.compile(r"旧版本(写|的)"),
     "磁盘数据可能由更早的版本写入，属必须考虑的兼容约束"),
    # 源码注释里指向另一个模块的路径（如 server.py、tools/policy.py）
    # 是工程上正常的交叉引用，不是作业记录。收进来会制造大量误报，
    # 因此打印出来接受审视，但不阻断。
    ("模块路径交叉引用", re.compile(r"[\w/]+\.py\b"),
     "源码注释中指向其它模块的位置，属工程常规"),
]


def _py_comment_lines(src: str) -> list[tuple[int, str]]:
    """取 Python 源码里的注释行与文档字符串行。"""
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string.lstrip("#").strip()))
    except Exception:
        pass
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    start = node.body[0].lineno
                    for off, line in enumerate(doc.split("\n")):
                        out.append((start + off, line.strip()))
    except Exception:
        pass
    return out


def _ts_comment_lines(src: str) -> list[tuple[int, str]]:
    """取 TypeScript / TSX 源码里的注释行。"""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(src.split("\n"), 1):
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            out.append((i, line.strip()))
    return out


def scan(root: Path) -> tuple[list[tuple[str, str, int, str]],
                              list[tuple[str, str, int, str, str]]]:
    fails: list[tuple[str, str, int, str]] = []
    allows: list[tuple[str, str, int, str, str]] = []

    targets: list[tuple[Path, str]] = []
    py_root = root / "omegaforge"
    if py_root.is_dir():
        targets += [(p, "py") for p in sorted(py_root.rglob("*.py"))]
    ts_root = root / "frontend" / "src"
    if ts_root.is_dir():
        targets += [(p, "ts") for p in sorted(ts_root.rglob("*.ts"))
                    + sorted(ts_root.rglob("*.tsx"))]
    # 测试文件随仓库一同发布，说明文字同样要求整洁。
    # 唯一例外是本守卫自身：它内部的样本是刻意构造的脏输入，用来证明
    # 扫描器确实会失败，必须排除，否则自己命中自己。
    tests_root = root / "tests"
    if tests_root.is_dir():
        targets += [(p, "py") for p in sorted(tests_root.rglob("*.py"))
                    if p.name != "test_comment_hygiene_guard.py"]
    # 检查脚本与脚本同样随仓库发布，说明文字要求一致。原先只扫上面三个目录，
    # 这两个目录里的章节引用与作业记录一条都查不到——扫描报"通过"并不
    # 代表全仓整洁，只代表被扫到的那部分整洁。
    for extra in ("probes", "scripts"):
        sub = root / extra
        if sub.is_dir():
            targets += [(p, "py") for p in sorted(sub.rglob("*.py"))
                        if p.name != "check_comment_hygiene.py"]

    for path, kind in targets:
        # 归一为正斜杠：相对路径的字符串形态随平台变化（Windows 为反斜杠），
        # 分类前缀、目录排除与终端显示的口径都依赖它，统一后两平台行为一致。
        rel = path.relative_to(root).as_posix()
        if "/ui/" in rel:
            continue  # 组件库源码，非本项目编写
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = (_py_comment_lines(src) if kind == "py"
                 else _ts_comment_lines(src))
        for lineno, text in lines:
            if not text:
                continue
            exempt = False
            for name, rx, reason in ALLOW_PATTERNS:
                if rx.search(text):
                    allows.append((name, rel, lineno, text[:120], reason))
                    exempt = True
            if exempt:
                continue
            # 分类只认正斜杠形态：rel 已归一为 posix，故此处用字面量。
            # 若改用 os.sep 拼接前缀，Windows 上 os.sep 为反斜杠，而
            # Path.relative_to 的字符串形态同样随平台变化——两段一起变看不出问题，
            # 但只要其中一处被改成硬编码斜杠，Windows 上这三个目录就会整体
            # 改用更严的源码词表并必然报红，而 Linux 本地永远复现不了。
            pats = (TEST_FAIL_PATTERNS
                    if rel.startswith(("tests/", "probes/", "scripts/"))
                    else FAIL_PATTERNS)
            for name, rx in pats:
                if rx.search(text):
                    fails.append((name, rel, lineno, text[:120]))
                    break
    return fails, allows


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT_DEFAULT)
    if not root.is_dir():
        print(f"扫描目录不存在：{root}")
        return 1

    fails, allows = scan(root)

    if allows:
        print(f"已确认豁免 {len(allows)} 处（有书面理由，不阻断）：")
        for name, path, lineno, text, reason in allows[:10]:
            print(f"  [{name}] {path}:{lineno}  ← {reason}")
            print(f"      {text}")
        print()

    if not fails:
        print(f"说明文字整洁度扫描通过 ✓（{root}）")
        return 0

    counter = Counter(n for n, _, _, _ in fails)
    print(f"发现 {len(fails)} 处内部痕迹残留：")
    for name, cnt in counter.most_common():
        print(f"  {name}: {cnt}")
    print()
    for name, path, lineno, text in fails[:40]:
        print(f"[{name}] {path}:{lineno}")
        print(f"    {text}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
