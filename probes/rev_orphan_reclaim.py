"""校验：孤儿任务归位的四个校验点。

每条注入之后强制 ast.parse 校验语法——否则"测试没跑起来"会被误读成
"抓到了"。

| 校验点 | 注入 | 期望 |
|------|------|------|
| A | 详情读取不归位 | 抓到（状态停在半路） |
| B | 列表读取不归位 | 抓到（两页结论不一致） |
| C | 归位时不设终态进度 | 抓到（进度退回 0） |
| D | 起任务时不登记活跃（防修过头） | 抓到（正在跑的被误判） |

运行：  python3 probes/rev_orphan_reclaim.py
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "omegaforge" / "core" / "run.py"
SERVER = ROOT / "omegaforge" / "server.py"
TEST = ROOT / "tests" / "test_orphan_run_reclaim.py"
C = "tests/test_orphan_run_reclaim.py::"
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

RUN_ORIG = RUN.read_text(encoding="utf-8")
SRV_ORIG = SERVER.read_text(encoding="utf-8")

ANCHORS = [
    ("A 详情读取不归位", RUN,
     '        if r._status not in TERMINAL and not is_active(r.id):',
     '        if False:',
     [C + "test_orphan_run_becomes_interrupted",
      C + "test_list_and_detail_agree",
      C + "test_orphan_progress_is_terminal_not_zero",
      C + "test_active_run_is_not_mistaken_for_orphan"]),
    # 校验点必须对准"真正生效的那段代码"。B 原先写的是
    # `status = phase = "interrupted"`，而实现后来改成只改 status、
    # 保留 phase（phase 记录实际停在哪一步，改掉会让用户看不出断点），
    # 于是该校验点变成"注入点不存在"，长期静默失效。
    ("B 列表读取不归位", RUN,
     '            if status not in TERMINAL and not is_active(name):\n'
     '                status = "interrupted"',
     '            if False:\n                pass',
     [C + "test_list_and_detail_agree"]),
    # C 原先改 PHASE_PROGRESS["interrupted"] 的数值，检验这是个死校验点：
    # 该键只在 `phase == "interrupted"` 且 status 非终态时被读到，而
    # emit(phase=...) 一旦落终态会同时置 status，"interrupted" 又属于
    # TERMINAL —— 两条条件不可能同时成立，改这个数恒无影响，校验点永远
    # 抓不到。真正决定"中断任务进度条到顶"的是 progress() 的终态优先。
    ("C 进度不以终态优先（中断任务退回半路进度）", RUN,
     '        if self._status in TERMINAL:\n            return 100',
     '        if False:\n            return 100',
     [C + "test_orphan_progress_is_terminal_not_zero"]),
    ("D 起任务时不登记活跃（防修过头）", SERVER,
     "    mark_active(jid)          # 本进程正在跑它 —— 重启后本登记天然消失",
     "    pass",
     [C + "test_server_marks_active_runs",
      C + "test_orphan_progress_is_terminal_not_zero"]),
]


def pytest_env() -> dict:
    """解析 pytest 运行环境（沙盒 /tmp 会被回收，不能硬编码路径）。"""
    import pathlib as _pl
    import sys as _s
    _root = str(_pl.Path(__file__).resolve().parent.parent)
    if _root not in _s.path:
        _s.path.insert(0, _root)
    from probes._pytest_env import env as _env
    return _env()


def run_test() -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。"""
    return pytest_run(str(TEST))


def main() -> int:
    try:
        rc, _f, tail = run_test()
        print(f"[基线] rc={rc}  {tail}")
    except subprocess.TimeoutExpired:
        print("[基线] 超时")
        return 1

    caught = total = 0
    for name, target, old, new, expect in ANCHORS:
        total += 1
        orig = RUN_ORIG if target is RUN else SRV_ORIG
        if old not in orig:
            # 锚点失效不能只打印一句就跳过：那样"零个校验点执行过"会被
            # 读成"验过且通过"。
            print(f"[{name}] 注入点不存在 —— 锚点失效，须重定位（记为未抓到）")
            continue
        patched = orig.replace(old, new, 1)
        try:
            ast.parse(patched)
        except SyntaxError as e:
            print(f"[{name}] 注入后语法损坏（不可作为证据）: {e}")
            continue
        target.write_text(patched, encoding="utf-8")
        try:
            rc, failed, tail = run_test()
        except subprocess.TimeoutExpired:
            print(f"[{name}] 超时")
            continue
        hit, why = verdict(rc, failed, expect)
        caught += 1 if hit else 0
        print(f"[{name}] rc={rc}  {tail}   → {'抓到' if hit else '未抓到'}"
              f"（{why}）")

    RUN.write_text(RUN_ORIG, encoding="utf-8")
    SERVER.write_text(SRV_ORIG, encoding="utf-8")
    rc, _f, tail = run_test()
    print(f"[还原] rc={rc}  {tail}")
    # 退出码必须真实反映结果：hit 算了却不累加、main 恒返回 0 时，
    # "零个校验点执行过"会被读成"验过且通过"。
    print(f"结论：{caught}/{total} 锚点抓到")
    return 0 if caught == total and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())