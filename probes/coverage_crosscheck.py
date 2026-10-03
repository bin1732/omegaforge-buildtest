"""交叉核实：函数级"未执行"到底是真盲区，还是检查脚本的可见性盲区。

检查脚本用 sys.settrace 统计，而 settrace 有两个已知盲区：
  · 子进程（subprocess 启动的 CLI / 服务）完全不可见
  · 新建线程不继承 tracer，线程内执行不可见

所以"未执行"不能直接当成"没测过"。本脚本对每个未执行函数，检查：
  1. tests/ 与 probes/ 里是否出现过该函数名（有测试引用）
  2. 引用它的测试是否走 subprocess / Thread（若是 → 大概率是检查脚本盲区）

输出三档：
  BLIND-REAL     无任何引用 → 真盲区，优先打
  BLIND-TRACE    有引用但走子进程/线程 → 检查脚本看不见，需换方式验证
  UNKNOWN        有引用且非子进程 → 需人工确认

运行：  python3 probes/coverage_crosscheck.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "omegaforge"
TESTS = ROOT / "tests"
PROBES = ROOT / "probes"

sys.path.insert(0, str(PROBES))
import coverage_tracer as ct  # noqa: E402

SUBPROC_RE = re.compile(r"subprocess|Thread\(|threading|multiprocessing|Popen")


def _scan_dir(d: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """目录内所有 .py 的文本索引（函数名 → 出现在哪些文件）。"""
    idx: dict[str, list[pathlib.Path]] = {}
    for p in sorted(d.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]{3,})", txt):
            idx.setdefault(m.group(1), []).append(p)
    return idx


def main() -> int:
    executed = ct.load_all()
    declared = ct.declared_functions()

    missing: list[tuple[str, str]] = []
    for path, names in sorted(declared.items()):
        for n in sorted(names):
            if (path, n) not in executed:
                missing.append((path, n))

    test_idx = _scan_dir(TESTS)
    probe_idx = _scan_dir(PROBES)
    src_idx = _scan_dir(PKG)

    buckets: dict[str, list[tuple[str, str, str]]] = {
        "BLIND-REAL": [], "BLIND-TRACE": [], "UNKNOWN": [],
    }
    for path, name in missing:
        rel = str(pathlib.Path(path).relative_to(ROOT))
        refs = set(test_idx.get(name, [])) | set(probe_idx.get(name, []))
        # 产品包内部的引用——“有没有人真的调用它”。
        # 同文件内也要算调用者（main() 调 _banner 就属此类），因此用
        # “出现次数 > 1”排除定义行本身；跨文件出现 1 次即为调用。
        callers: list[str] = []
        for p in sorted(PKG.rglob("*.py")):
            if "__pycache__" in str(p):
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits = len(re.findall(rf"\b{re.escape(name)}\b", txt))
            if str(p) == path:
                if hits > 1:
                    callers.append(p.name + "(同文件)")
            elif hits >= 1:
                callers.append(p.name)
        if not refs:
            if callers:
                buckets["BLIND-REAL"].append(
                    (rel, name, "无测试引用；被 " + ",".join(callers[:3]) + " 调用"))
            else:
                buckets["BLIND-REAL"].append((rel, name, "无测试引用；全仓无调用者"))
            continue
        subproc = []
        for r in sorted(refs):
            try:
                txt = r.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if SUBPROC_RE.search(txt):
                subproc.append(r.name)
        if subproc:
            buckets["BLIND-TRACE"].append((rel, name, "子进程/线程: " + ",".join(subproc[:2])))
        else:
            buckets["UNKNOWN"].append((rel, name, "引用: " + ",".join(sorted(r.name for r in refs)[:2])))

    print(f"未执行函数总数 {len(missing)}")
    for k in ("BLIND-REAL", "BLIND-TRACE", "UNKNOWN"):
        print(f"\n=== {k}  {len(buckets[k])} ===")
        for rel, name, why in buckets[k]:
            print(f"  {rel}::{name}   [{why}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
