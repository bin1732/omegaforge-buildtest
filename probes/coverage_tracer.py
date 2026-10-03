"""真实动态覆盖度检查脚本。

统计每个后端函数在全量测试中**是否真的被执行过**。

思路：用 sys.settrace 挂一个全局追踪函数，在每次函数调用事件时记录
(文件, 函数名)。跑完测试后，把「AST 里声明的全部函数」与「实际执行过的
函数」做差集——差集就是没有被任何测试触达的函数。

与「文档提到次数」这类代理指标的区别：本检查脚本看的是**运行时事实**，
不看文档，因此不会把「写进过某章」误当成「被测过」。

支持分批累积：每批跑完把结果写进 JSON，最后 merge 汇总。这样单次
命令超时也不会丢数据。

用法
----
  # 跑一批并累积
  python3 probes/coverage_tracer.py --out probes/.cov/run1.json tests/foo.py
  # 汇总所有批次
  python3 probes/coverage_tracer.py --merge

局限（说明）
----------------
· 只在本次跑的那批测试下统计；未跑到的测试文件不计入。
· 覆盖到函数粒度，不做分支/行覆盖。
· 装饰器包装的函数、生成器、lambda 会有少量统计偏差。
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "omegaforge"
COVDIR = ROOT / "probes" / ".cov"

_executed: set[tuple[str, str]] = set()


_pkg_cache: dict[str, bool] = {}


def _is_pkg_file(filename: str) -> bool:
    """判定是否属于产品包。

    这里必须缓存：追踪器对**每一次函数调用**都会调用本函数，而路径解析
    是系统调用级别的开销。不缓存会让整批测试慢到无法在时限内跑完——
    这正是第一版检查脚本超时无输出的原因。
    """
    cached = _pkg_cache.get(filename)
    if cached is not None:
        return cached
    try:
        p = pathlib.Path(filename).resolve()
        ok = str(p).startswith(str(PKG)) and p.suffix == ".py"
    except Exception:                                # noqa: BLE001
        ok = False
    _pkg_cache[filename] = ok
    return ok


def _tracer(frame, event, arg):                      # noqa: ANN001
    if event == "call" and _is_pkg_file(frame.f_code.co_filename):
        _executed.add((frame.f_code.co_filename, frame.f_code.co_name))
    return None


def declared_functions() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        out[str(p)] = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    return out


def run_pytest(argv: list[str]) -> int:
    import pytest

    sys.settrace(_tracer)
    try:
        rc = pytest.main(argv)
    finally:
        sys.settrace(None)
    return int(rc)


def save(out: pathlib.Path) -> None:
    COVDIR.mkdir(parents=True, exist_ok=True)
    data = sorted(f"{p}::{n}" for p, n in _executed)
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_all() -> set[tuple[str, str]]:
    acc: set[tuple[str, str]] = set()
    if not COVDIR.exists():
        return acc
    for f in COVDIR.glob("*.json"):
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for it in items:
            p, _, n = it.rpartition("::")
            acc.add((p, n))
    return acc


def report(acc: set[tuple[str, str]]) -> None:
    declared = declared_functions()
    total_fn = sum(len(v) for v in declared.values())
    rows = []
    for path, names in sorted(declared.items()):
        hit = {n for n in names if (path, n) in acc}
        rows.append((path, names, hit))

    hit_fn = sum(len(r[2]) for r in rows)
    print(f"函数总数 {total_fn}   已执行 {hit_fn}   未执行 {total_fn - hit_fn}")
    if total_fn:
        print(f"整体函数覆盖率 {hit_fn / total_fn * 100:.1f}%")
    print()
    print(f"{'文件':<50} {'函数':>4} {'已跑':>4} {'未跑':>4}")
    print("-" * 68)
    for path, names, hit in sorted(rows, key=lambda r: len(r[1] - r[2]), reverse=True):
        rel = str(pathlib.Path(path).relative_to(ROOT))
        print(f"{rel:<50} {len(names):>4} {len(hit):>4} {len(names - hit):>4}")

    print()
    print("=== 未执行函数最多的 6 个文件（明细）===")
    for path, names, hit in sorted(rows, key=lambda r: len(r[1] - r[2]), reverse=True)[:6]:
        miss = sorted(names - hit)
        if not miss:
            continue
        rel = str(pathlib.Path(path).relative_to(ROOT))
        print(f"\n[{rel}]  未跑 {len(miss)}/{len(names)}")
        for m in miss[:40]:
            print(f"    {m}")


def main() -> int:
    argv = sys.argv[1:]
    if "--merge" in argv:
        report(load_all())
        return 0
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = pathlib.Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        argv = ["tests/", "-q", "--no-header", "-p", "no:cacheprovider"]
    run_pytest(argv)
    if out:
        save(out)
        print(f"[saved] {out}  {len(_executed)} 条")
    else:
        report(_executed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
