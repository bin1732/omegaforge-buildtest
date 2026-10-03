"""校验：把修复逐条撤掉，确认守卫真的抓得到（不是空转）。

校验点：
  A  取消唯一临时文件名（回到 path + ".tmp"）      → 并发写产出损坏文件
  B  取消跨进程锁与写前重读（回到 ensure_loaded）   → 并发/跨进程丢更新
  C  取消 file_lock 的进程内引用计数                → 嵌套调用自锁（死锁）

用法：python scripts/reverse_concurrency.py
每个校验点独立：改 -> 跑 -> 还原 -> 下一个。任一校验点没抓到就退出非 0。
"""
from __future__ import annotations

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = "tests/test_storage_concurrency.py"

ATOMICIO = os.path.join(ROOT, "omegaforge/core/atomicio.py")
TASKS = os.path.join(ROOT, "omegaforge/memory/tasks.py")
KB = os.path.join(ROOT, "omegaforge/memory/kb.py")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def _sub(path, old, new, label):
    s = _read(path)
    if s.count(old) != 1:
        print(f"  !! 锚点 {label}: 期望 1 处，实际 {s.count(old)} 处")
        return False
    _write(path, s.replace(old, new, 1))
    return True


def run_tests():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=env, timeout=900)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(无输出)"
    return r.returncode, tail


def anchor(label, edits, expect_min_fail):
    print(f"\n=== 锚点 {label} ===")
    backups = {p: _read(p) for p, _, _, _ in edits}
    try:
        ok = True
        for p, old, new, tag in edits:
            if not _sub(p, old, new, tag):
                ok = False
        if not ok:
            return False
        rc, tail = run_tests()
        print(f"  pytest 退出码 {rc}：{tail}")
        m = re.search(r"(\d+) failed", tail)
        failed = int(m.group(1)) if m else 0
        if rc != 0 and failed >= expect_min_fail:
            print(f"  ✓ 抓到（{failed} 项失败）")
            return True
        print(f"  ✗ 没抓到（失败 {failed} 项，期望 >= {expect_min_fail}）——守卫是摆设")
        return False
    finally:
        for p, s in backups.items():
            _write(p, s)
        print("  （已还原源码）")


def main():
    results = {}

    # A 固定临时文件名：并发写同一路径必然产出交错/拼接体
    results["A 唯一临时文件名"] = anchor("A", [
        (ATOMICIO,
         '    base = os.path.basename(path)\n    return os.path.join(\n'
         '        os.path.dirname(path) or ".",\n'
         '        f".{base}.{os.getpid()}.{threading.get_ident()}'
         '.{uuid.uuid4().hex[:8]}.tmp",\n    )',
         '    return path + ".tmp"', "A"),
    ], expect_min_fail=1)

    # B 取消跨进程锁与写前重读
    results["B 跨进程锁+重读"] = anchor("B", [
        (TASKS,
         '''        with self._lock:
            with file_lock(self.path):
                self._reload()
                self._loaded_home = self.home
                yield''',
         '''        with self._lock:
            self.ensure_loaded()
            yield''', "B-tasks"),
        (KB,
         '''        with self._lock:
            with file_lock(self.path):
                self._reload()
                self._loaded_home = self.home
                yield''',
         '''        with self._lock:
            self.ensure_loaded()
            yield''', "B-kb"),
    ], expect_min_fail=2)

    # C 取消进程内引用计数 -> remember() 内部再调 add() 会自锁
    results["C 锁可重入"] = anchor("C", [
        (ATOMICIO, "        if n:", "        if False:  # noqa: SIM102", "C"),
    ], expect_min_fail=1)

    print("\n=== 汇总 ===")
    bad = [k for k, v in results.items() if not v]
    for k, v in results.items():
        print(f"  {'抓到' if v else '没抓到'}  {k}")
    if bad:
        print(f"\n有 {len(bad)} 个锚点没抓到，守卫存在空洞")
        return 1
    print("\n全部锚点抓到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
