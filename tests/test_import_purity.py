# -*- coding: utf-8 -*-
"""测试模块的「导入纯净度」守卫。

## 为什么必须有这一层

`tests/test_server_compare_runs.py::test_real_distill_persists_fingerprint`
的表现是：**单独跑 12/12 全过，整批跑必红**，连跑三次单独跑都绿。

二分到最后才发现真凶是**另一个文件**：`tests/test_sse_body.py` 是"独立脚本"
风格——模块顶层直接起两个 HTTP 服务、写 `providers.json`、把
`OMEGAFORGE_HOME` 劫持到自己的临时目录。只要它被 **import** 一次（不需要执行
任何用例），全局就被改写了；而 pytest 在【收集阶段】就会 import 所有匹配文件。

原设计靠 `conftest.collect_ignore` 排除它。但 `collect_ignore` **只在目录递归
时生效**——命令行上显式点名/通配时 pytest 照样收集：

  pytest tests/      全绿（sse_body 被 ignore）
  pytest tests/test_s*.py 必红（shell 通配把它显式点名进来）

`tests/__pycache__/` 里存在这四个模块的 pyc，证明该隐患缺少该约束时真实发生过。

所以判据不是"有没有写 collect_ignore"，而是：**任何被 pytest 收集的模块，
import 时不得产生副作用**。改名 `_legacy_*.py` 是治本（`test_*.py` 通配永远
捡不到），本文件是占位（新写的测试不得再犯）。

## 三条禁令（全部对应真实失效，不是风格偏好）

1. 模块级改写 `os.environ`
  —— 验证：sse_body 改 HOME → 后续用例拿到被劫持的配置；
   test_genome_report_api 设 `OF_MOCK=1`，而全仓**没有任何代码读它**
   （"写着开启 mock，实际没有任何代码读它"——与"写了守卫没人执行"同族）。
2. 模块级起服务 / 起线程 / 调 sys.exit / 调 pytest.main
  —— 收集阶段就把端口占住、把套件跑完，后续模块的用例数全部失真。
3. 模块级写文件（providers.json / SKILL.md 之类用户数据）
  —— 收集阶段就往用户数据目录落盘。

`if __name__ == "__main__":` 块内的同类语句**放行**：那是 `python3 xxx.py`
独立运行的入口，pytest 不会执行它。
"""

from __future__ import annotations

import ast
import pathlib


TESTS = pathlib.Path(__file__).resolve().parent

# 允许出现在 if __name__ == "__main__" 之外的模块级语句：注释/导入/常量定义/函数类
_SIDE_EFFECT_CALLS = (
  "ThreadingHTTPServer", "HTTPServer", "serve_forever", "Thread(",
  "sys.exit", "pytest.main", "unittest.main",
)

_FILE_WRITE_HINTS = ("providers.json", "SKILL.md", "MEMORY.md", "AGENTS.md")


def _module_level_nodes(path: pathlib.Path):
  """产出模块级（不在 if __name__ == "__main__" 内）的语句节点。"""
  src = path.read_text(encoding="utf-8")
  tree = ast.parse(src, filename=str(path))
  for node in tree.body:
    # 跳过 `if __name__ == "__main__":` —— 那是独立运行入口
    if isinstance(node, ast.If):
      test = ast.unparse(node.test)
      if "__name__" in test and "__main__" in test:
        continue
    yield node, src


def _violations(path: pathlib.Path) -> list[str]:
  out: list[str] = []
  for node, src in _module_level_nodes(path):
    seg = (ast.get_source_segment(src, node) or "").replace("\n", " ")
    seg = seg[:120]

    # 1) 模块级改写 os.environ
    if isinstance(node, ast.Assign):
      for t in node.targets:
        if isinstance(t, ast.Subscript) and "environ" in ast.unparse(t.value):
          out.append(f"模块级改写环境变量: {seg}")
    # os.environ.setdefault(...) / update(...)
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
      fn = ast.unparse(node.value.func)
      if "environ" in fn and any(
          m in fn for m in ("setdefault", "update", "pop", "clear")):
        out.append(f"模块级改写环境变量: {seg}")

    # 2) 模块级起服务 / 起线程 / 退出
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
      fn = ast.unparse(node.value.func)
      if any(k in fn for k in _SIDE_EFFECT_CALLS):
        out.append(f"模块级副作用调用: {seg}")

    # 3) 模块级写用户数据文件
    if isinstance(node, ast.With) and any(h in seg for h in _FILE_WRITE_HINTS):
      out.append(f"模块级写用户数据文件: {seg}")
  return out


def _collected_test_modules() -> list[pathlib.Path]:
  """所有会被 pytest 收集的测试模块（conftest.py 也在此列）。"""
  return sorted(TESTS.glob("test_*.py"))


def test_no_module_level_side_effects_in_collected_modules():
  bad: dict[str, list[str]] = {}
  for p in _collected_test_modules():
    if p.name == pathlib.Path(__file__).name:
      continue # 本文件自身只有函数定义，跳过以免自指
    v = _violations(p)
    if v:
      bad[p.name] = v

  assert not bad, (
    "以下测试模块在【import 时】产生副作用。pytest 在收集阶段就会 import "
    "所有匹配文件，因此这些副作用与'有没有执行它的用例'无关，只与"
    "'怎么调用 pytest' 有关——正是'单独跑全绿、整批跑必红'的成因。\n"
    + "\n".join(f" · {k}\n   {chr(10).join('   - ' + x for x in v)}"
           for k, v in bad.items())
  )


def test_legacy_scripts_are_not_collectable():
  """四个独立脚本必须叫 `_legacy_*.py`，永不被 `test_*.py` 通配捡到。

  只看"有没有写 collect_ignore"不够——验证 collect_ignore 对显式点名无效
  （`pytest tests/test_s*.py` 照样收集）。改名才是与调用方式无关的解法。
  """
  legacy = sorted(TESTS.glob("_legacy_*.py"))
  assert legacy, "tests/ 下应有 _legacy_*.py 独立脚本（被子进程驱动的套件）"

  import glob
  import subprocess
  import sys

  # 复现当初的现场：shell 先把 `tests/test_s*.py` 展开成文件列表再传给
  # pytest（pytest 自己不展开 glob，直接传 test_*.py 会报
  # "file or directory not found"）。必须显式列出文件，才是真实调用形态。
  explicit = sorted(glob.glob(str(TESTS / "test_*.py")))
  assert explicit, "tests/ 下没有匹配 test_*.py 的模块"

  for argv, label in (
      ([str(TESTS)], "pytest tests/"),
      (explicit, "pytest tests/test_*.py（shell 展开）"),
  ):
    p = subprocess.run(
      [sys.executable, "-m", "pytest", *argv, "--collect-only", "-q"],
      cwd=str(TESTS.parent), capture_output=True, text=True, timeout=180,
    )
    assert p.returncode == 0, (
      f"{label} 收集失败（rc={p.returncode}）：{p.stdout[-1200:]}")
    for f in legacy:
      assert f.name not in p.stdout, (
        f"{f.name} 仍被 `{label}` 收集到。"
        f"它一旦被 import 就会在收集阶段起服务/改环境，"
        f"使后续模块拿到被污染的全局——必须改名保持不可收集。")


def test_legacy_suite_count_matches_files():
  """SUITES 登记的脚本数必须等于磁盘上的 _legacy_*.py 数 + 其它脚本数。

  登记项被删（或新增未登记）会让"用例被静默删掉"的检测形同虚设。
  """
  import re
  src = (TESTS / "test_legacy_suites.py").read_text(encoding="utf-8")
  m = re.search(r"SUITES = \[(.*?)\]", src, re.S)
  assert m, "test_legacy_suites.py 里找不到 SUITES 列表"
  names = re.findall(r'\(\s*"([^"]+\.py)"', m.group(1))
  assert names, "SUITES 为空"
  for n in names:
    assert (TESTS / n).exists(), f"SUITES 登记的 {n} 在磁盘上不存在"
  # 每个 _legacy_*.py 都必须被登记，否则它永远不会被执行
  for f in TESTS.glob("_legacy_*.py"):
    assert f.name in names, (
      f"{f.name} 未被 SUITES 登记 —— 它不会被 pytest 收集，"
      f"也不在子进程套件里，等于一份从不执行的守卫。")
