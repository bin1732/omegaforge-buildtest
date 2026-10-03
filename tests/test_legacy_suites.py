"""把「独立脚本式」审计套件接进 pytest。

这三个文件是早期写法：模块顶层直接跑完全部用例并在末尾 sys.exit()，
没有 test_ 函数。缺少该约束时 conftest 直接 collect_ignore 掉它们，后果是——
**在 CI 里 pytest 全程跳过这 105 项检查，而没有任何提示**。

守卫写了但不执行，等于没写。这正是「反复修、反复漏」的结构性原因之一：
改完代码跑 pytest 全绿，其实这 105 项根本没跑过。

这里用子进程包装（而不是把它们重构成 main()），好处是：
 · 不动原文件，避免重构引入回归
 · 每个子套件独立进程，天然免疫全局变量/端口互相污染
 · 断言退出码，脚本内部已有的逐项 check 全部照常生效
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

# (脚本名, 期望通过项数) —— 项数写在这里是为了让"套件被偷偷删用例"也能被发现
SUITES = [
  ("_legacy_all_features.py", 55),
  ("_legacy_chat_suite.py", 18),
  # 33 → 34：门禁那轮新增 16.3b「confirm 档下写工具需确认」。
  # 这里同步登记是必要的——不登记则套件新增用例后，本文件的用例数
  # 断言会一直按旧数字匹配不上，形同虚设（用例被删也发现不了）。
  ("_legacy_product_suite.py", 34),
  ("test_chat_path_parity.py", 8),
  ("_legacy_sse_body.py", 14),
  ("test_cli_gate.py", 26),
]


def _run(name: str) -> subprocess.CompletedProcess:
  env = dict(os.environ)
  # 必须继承父进程的 PYTHONPATH（而不只是 ROOT）：依赖可能被装在非标准
  # 位置（沙盒里 pytest 一直在 /data/workspace/.pypkgs）。只写 ROOT 会
  # 让子套件在父进程能跑的情况下仍然 ModuleNotFoundError —— 于是"全绿"
  # 是假的：父进程收集到了、但子套件一条没执行。
  parts = [p for p in (os.environ.get("PYTHONPATH", ""), str(ROOT)) if p]
  env["PYTHONPATH"] = os.pathsep.join(parts)
  return subprocess.run(
    [sys.executable, str(TESTS / name)],
    cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300,
  )


def _mk(name: str, expect: int):
  def _t() -> None:
    p = _run(name)
    assert p.returncode == 0, (
      f"{name} 未通过（rc={p.returncode}）\n"
      f"--- stdout ---\n{p.stdout[-4000:]}\n"
      f"--- stderr ---\n{p.stderr[-2000:]}"
    )
    # 防"用例被静默删掉"：两种历史写法都要认——
    #  老脚本自己打印「通过 N/M」；走 pytest.main 的新脚本打印「N passed」。
    # 只认其中一种会让另一类套件的"用例数变了"永远不会被发现。
    hit = (f"/{expect} " in p.stdout
        or f"{expect}/{expect}" in p.stdout
        or f"{expect} passed" in p.stdout)
    assert hit, (
      f"{name} 用例数发生变化（期望 {expect} 项），"
      f"未命中则可能是用例被删除或新增未登记\n{p.stdout[-1500:]}"
    )
  return _t


for _name, _expect in SUITES:
  # 名字去掉 _legacy_ 前缀，避免生成 test_legacy_legacy_all_features 这种
  # 叠词；子套件的真实路径仍由 SUITES 里的脚本名决定。
  _key = _name[:-3].removeprefix("_legacy_").removeprefix("test_")
  globals()[f"test_legacy_{_key}"] = _mk(_name, _expect)
