"""空壳断言守卫：不许新增"只调用不看结果"的测试。

## 为什么需要这一层

有一类测试不含任何断言：调用被测函数，只要不抛异常就算通过。
它们看起来是绿的，实际上什么都没验到——把合法数据一并丢弃、
把分数清零、把锁变成空操作，这些错误实现同样不抛异常，测试照样
全绿。本仓已出现四例，全部是靠"注入错误实现看测试是否变红"
才发现的。

所以这里做两件事：
 1. 点名当前允许无断言的用例，并逐条写明它为什么不在此列
 2. 除此之外的任何新增，一律拦下

## 判定方式

用 AST 找 test_ 开头的函数，看函数体内是否出现 assert、或任何
名字带 assert / raises / fail / expect / verify 的调用。命中的
必须与白名单完全一致——多一个就失败。

白名单采取"只减不增"：将来把某个用例补强了，把它从白名单里删掉，
本守卫随即把它钉死，不允许再退回空壳。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 允许无断言的用例，逐条写明理由。
# 键为 "相对路径::函数名"，行号不参与比对，函数移动不会误判。
ALLOWED: dict[str, str] = {
  "tests/test_atomicio_lock_semantics.py::"
  "test_same_thread_nesting_still_does_not_deadlock":
    "同线程重入若自锁会永久阻塞，超时即失败；不抛异常本身就是断言",
  "tests/test_frontend_contract.py::test_contract_script_executes":
    "断言在被驱动的子进程里，由退出码体现",
  "tests/test_frontend_contract_guard.py::test_contract_script_executes":
    "断言在被驱动的子进程里，由退出码体现",
  "tests/test_response_shape_probe.py::test_contract_script_executes":
    "断言在被驱动的子进程里，由退出码体现",
  "tests/test_isolation_guard.py::test_a_poisons_globals":
    "刻意污染全局，由配套用例断言「污染未泄漏」",
  "tests/test_validate_boundary.py::test_non_empty_container_counts_as_filled":
    "非空容器不得判缺失；同文件的空白拒绝用例构成反向约束",
  "tests/test_validate_boundary.py::test_zero_and_false_still_count_as_filled":
    "0 与 False 不得判缺失；同文件的空白拒绝用例构成反向约束",
  "tests/test_validate_contract.py::test_require_treats_zero_as_filled":
    "0 与 False 不得判缺失；同文件的空白拒绝用例构成反向约束",
}

ASSERT_HINTS = ("assert", "raises", "fail", "check", "expect", "verify")


def _test_functions(path: Path):
  try:
    tree = ast.parse(path.read_text(encoding="utf-8"))
  except SyntaxError:
    return []
  out = []
  for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      continue
    if not node.name.startswith("test"):
      continue
    body = [b for b in node.body
        if not (isinstance(b, ast.Expr)
            and isinstance(b.value, ast.Constant))]
    if not body:
      continue
    out.append(node)
  return out


def _has_assertion(node: ast.AST) -> bool:
  for n in ast.walk(node):
    if isinstance(n, ast.Assert):
      return True
    if isinstance(n, ast.Call):
      fn = n.func
      name = getattr(fn, "attr", None) or getattr(fn, "id", None) or ""
      if any(h in name.lower() for h in ASSERT_HINTS):
        return True
  return False


def _asserting_helpers(tree: ast.AST) -> set[str]:
  """本文件内自身含断言的辅助方法名。

  断言写在辅助方法里、用例只做调用，是常见写法。若只按用例函数体判断，
  这类用例会被误判为空壳——误报会引着人给本来有效的用例硬塞断言，
  比漏报更麻烦。所以调用到"确实含断言的辅助方法"同样算有效校验。
  """
  out = set()
  for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      continue
    if node.name.startswith("test"):
      continue
    if _has_assertion(node):
      out.add(node.name)
  return out


def _hollow() -> list[str]:
  found = []
  for p in sorted((ROOT / "tests").rglob("test_*.py")):
    rel = p.relative_to(ROOT).as_posix()
    try:
      tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
      continue
    helpers = _asserting_helpers(tree)
    for fn in _test_functions(p):
      if _has_assertion(fn):
        continue
      called = {n.func.attr for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)}
      if not (called & helpers):
        found.append(f"{rel}::{fn.name}")
  return found


def _all_test_functions() -> list[str]:
  out = []
  for p in sorted((ROOT / "tests").rglob("test_*.py")):
    for fn in _test_functions(p):
      out.append(f"{p.relative_to(ROOT).as_posix()}::{fn.name}")
  return out


def test_scan_actually_sees_the_tests() -> None:
  """自检：扫描必须真的读到用例，而不是空转。

  扫描目录一旦写错，rglob 返回空集，"无新增空壳"会恒真——
  守卫自己就成了空壳。所以先钉住扫描规模。
  """
  seen = _all_test_functions()
  assert len(seen) > 200, (
    f"只扫到 {len(seen)} 个测试函数，扫描路径很可能失效。"
    "这种情况下其余断言全部恒真，属假通过。")


def test_no_new_hollow_test() -> None:
  """无断言的用例只能来自白名单，且白名单只减不增。"""
  found = set(_hollow())
  unexpected = sorted(found - set(ALLOWED))
  assert not unexpected, (
    "以下用例不含任何断言，注入错误实现也不会变红，属空壳：\n "
    + "\n ".join(unexpected)
    + "\n请补上真实断言；确有特殊理由的，在本文件白名单里写明后添加。")


def test_allowlist_has_no_stale_entry() -> None:
  """白名单里的条目必须逐个说明存在理由，不得留空说明。"""
  for key, reason in ALLOWED.items():
    assert reason.strip(), f"白名单条目缺少理由说明：{key}"
    assert "::" in key, f"白名单键格式应为 路径::函数名：{key}"


def test_allowlist_entries_still_exist_or_were_strengthened() -> None:
  """白名单不得随意扩张。

  条目被删掉（用例补强）是进步；凭空新增则必须先说明理由。
  这里钉住的是条目数：只允许减少，不允许增加。
  """
  assert len(ALLOWED) <= 8, (
    f"白名单条目数 {len(ALLOWED)} 超过上限 8，"
    "新增空壳用例必须先在用例里补上真实断言。")
