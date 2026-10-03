"""错误码契约守卫（pytest，CI 会真实执行）。

为什么需要：
 后端 omegaforge/core/errors.py 定义 CODE_* 常量，
 前端 frontend/src/lib/format.ts 有一张 CODE_TEXT 映射表。
 两边靠**字符串**对齐，没有任何编译期约束——后端加一个码而前端没补，
 用户就会看到代码里的兜底文案，且不会有任何报错提示。

验证踩到的坑（本文件存在的理由）：
 PermissionError 被 _classify 归成「读写失败，请检查运行目录权限」，
 而系统能力未授权也抛 PermissionError（语义完全不同）。
 用户因此被引导去查磁盘权限，实际只是设置里的开关没开。

本守卫做两件事：
 1. 后端每个 CODE_* 必须在前端有中文文案
 2. 前端每个 key 必须在后端存在（防止前端留着已废弃的码）
"""
from __future__ import annotations

import os
import tempfile
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FRONTEND_FORMAT = os.path.join(ROOT, "frontend", "src", "lib", "format.ts")


def _backend_codes() -> set[str]:
  from omegaforge.core import errors
  return {
    v for k, v in vars(errors).items()
    if k.startswith("CODE_") and isinstance(v, str)
  }


def _frontend_codes() -> set[str]:
  if not os.path.isfile(FRONTEND_FORMAT):
    return set()
  src = open(FRONTEND_FORMAT, encoding="utf-8").read()
  # 只取 CODE_TEXT 块，避免把 HTTP_TEXT 的数字键混进来
  m = re.search(r"const CODE_TEXT[^=]*=\s*\{(.*?)\n\}", src, re.S)
  if not m:
    return set()
  return set(re.findall(r"(E_[A-Z_]+):", m.group(1)))


def test_oserror_family_is_not_all_server_fault():
  """_classify 曾按**类型名**白名单匹配 OSError 家族，只列了 2 个成员。

  缺少该约束时穿透到兜底、被说成「操作失败，请稍后重试」的五种：

    NotADirectoryError → --out 填成了已存在的文件
    IsADirectoryError  → 把目录当文件读
    FileExistsError   → 目录已存在
    BlockingIOError   → 资源被占用
    BrokenPipeError   → 连接断开

  五种全都是**用户当场就能改**的情形，却与真实的服务端故障共用同一句文案。
  而 PermissionError / OSError 恰好在白名单内——同一个继承链上一半对一半错，
  靠读代码极难发现，必须靠枚举验证。

  改为 isinstance 判定后，新出现的 OSError 子类会自动落进兜底而非伪装成 500。
  """
  from omegaforge.core import errors

  def _mk(fn):
    try:
      fn()
    except Exception as e:   # noqa: BLE001
      return e
    raise AssertionError("构造异常失败")

  tmp = tempfile.mkdtemp()
  f = os.path.join(tmp, "a_file")
  with open(f, "w", encoding="utf-8") as fh:
    fh.write("x")

  cases = {
    "NotADirectoryError": _mk(lambda: os.makedirs(os.path.join(f, "sub"))),
    "IsADirectoryError": _mk(lambda: open(tmp)),
    "FileExistsError": _mk(lambda: os.makedirs(tmp, exist_ok=False)),
    "BlockingIOError": _mk(
      lambda: (_ for _ in ()).throw(BlockingIOError("lock"))),
    "BrokenPipeError": _mk(
      lambda: (_ for _ in ()).throw(BrokenPipeError("pipe"))),
  }
  for name, exc in cases.items():
    _code, msg = errors._classify(exc)
    assert msg != errors._DEFAULT_MSG, (
      f"{name} 仍被映射成系统故障文案「{msg}」——"
      f"这是用户可当场修正的路径问题，不该与真实故障同文案")
    assert msg, f"{name} 映射出了空文案"

  # errno 级判别：磁盘满与路径过长，同样不该是 500 级模糊提示
  _c1, m1 = errors._classify(OSError(28, "No space left on device"))
  assert "磁盘空间" in m1
  _c2, m2 = errors._classify(OSError(36, "File name too long"))
  assert "路径过长" in m2

  # 自定义异常不能被新分支误截获（CapabilityDenied 继承自 PermissionError）
  from omegaforge.tools.system_tools import (BlockedCommand,
                        CapabilityDenied,
                        McpScopeDenied)
  for exc, needle in [
    (BlockedCommand("rm -rf / 属于破坏性命令"), "破坏性"),
    (CapabilityDenied("web_fetch"), "需要授权"),
    (McpScopeDenied("fs_write"), "MCP 作用域"),
  ]:
    _code, msg = errors._classify(exc)
    assert needle in msg, (
      f"{type(exc).__name__} 被 OSError 分支截获：{msg}")


def test_backend_codes_have_frontend_text():
  be, fe = _backend_codes(), _frontend_codes()
  assert be, "未能从 errors.py 提取到任何 CODE_*"
  missing = sorted(be - fe)
  assert not missing, (
    f"后端错误码缺少前端中文文案，用户会看到兜底文案: {missing}\n"
    f"请在 {os.path.relpath(FRONTEND_FORMAT, ROOT)} 的 CODE_TEXT 中补齐"
  )


def test_frontend_codes_exist_in_backend():
  be, fe = _backend_codes(), _frontend_codes()
  stale = sorted(fe - be)
  assert not stale, f"前端存在后端已废弃的错误码: {stale}"


def test_capability_denied_is_distinct_from_fs_permission():
  """未授权 ≠ 磁盘权限错误。"""
  from omegaforge.core.errors import CODE_PERMISSION, _classify
  from omegaforge.tools.system_tools import CapabilityDenied

  code, msg = _classify(CapabilityDenied("terminal"))
  assert code == CODE_PERMISSION, f"未授权被误判为 {code}"
  assert "授权" in msg, f"文案没有引导用户去开授权: {msg}"
  assert "读写失败" not in msg, "未授权不应提示磁盘权限问题"

  code2, _ = _classify(PermissionError("denied"))
  assert code2 != CODE_PERMISSION, "真实文件系统权限错误不应复用授权错误码"


def test_no_hardcoded_code_literals_outside_errors_module():
  """后端不得在 errors.py 之外硬编码 "E_XXX" 字面量。

  为什么需要（本条是验证踩出来的，不是预防性想象）：
   server.py 曾直接写 {"code": "E_PLAN"}，而 errors.py 里根本没有
   CODE_PLAN 常量。后果是——契约守卫判定"前端有 E_PLAN、后端无"而报红，
   但真正的病灶不是前端多写了，而是后端把这个码写成了裸字符串，
   绕过了唯一的可枚举来源。若只改前端删掉 E_PLAN，
   计划模式就会退化成"没有码"，用户看到的是兜底文案。

  所以正确修法是补常量、换引用，并用本守卫锁死：
  以后谁再写裸 "E_XXX"，CI 直接失败。
  """
  import subprocess

  pkg_dir = os.path.join(ROOT, "omegaforge")
  out = subprocess.run(
    ["grep", "-rn", "--include=*.py", r'"E_[A-Z_]\+"', pkg_dir],
    capture_output=True, text=True)
  # grep 无匹配时返回 1
  hits = [ln for ln in out.stdout.splitlines() if ln.strip()]
  stale = [ln for ln in hits
       if os.sep + "errors.py" not in ln and "/errors.py" not in ln]
  assert not stale, (
    "后端存在硬编码错误码字面量，必须改用 errors.py 的 CODE_* 常量：\n"
    + "\n".join(stale))
