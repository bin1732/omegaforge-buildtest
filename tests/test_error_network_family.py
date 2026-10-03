"""网络异常家族映射守卫。

为什么需要这个文件
--------------------
`core/errors.py` 的 `_classify` 决定**用户看到哪句话**。它早年按
**类型名白名单**匹配，缺少该约束时枚举出六种连接类异常被误报成
**磁盘权限问题**：

  ConnectionResetError  -> 读写失败，请检查运行目录权限
  RemoteDisconnected   -> 读写失败，请检查运行目录权限
  ConnectionAbortedError -> 读写失败，请检查运行目录权限
  OSError(EHOSTUNREACH)  -> 读写失败，请检查运行目录权限
  ContentTooShortError  -> 读写失败，请检查运行目录权限
  IncompleteRead     -> 操作失败，请稍后重试

这六种都是"连接断了 / 主机不可达 / 响应不完整"，用户当场能做的是检查
网络与接口地址，却被引导去查目录权限——**重试一百次也不会成功**，属于
误导性文案，比直接报错更糟。

这跟 OSError 家族当年的白名单 bug 完全同源：按名字匹配，同一条继承链上
一半对一半错（`ConnectionRefusedError` 在名单里、`ConnectionResetError`
不在），靠读代码极难发现，只能验证枚举。

守卫三件事
----------
1. 连接类异常 → 必须是网络语义，**绝不能**是磁盘权限文案
2. 磁盘类异常 → 必须保持原样（防处理过头）
3. 五个自定义异常 → 映射未被网络分支截获（网络分支插在它们之前）

回退校验点见 `probes/rev_network_family.py`（本文件末尾的 REVERT_ANCHORS
只作索引，真正执行的是那个脚本；两者不一致时以脚本为准）。
"""
from __future__ import annotations

import errno
import http.client
import json
import os
import socket
import ssl
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omegaforge.core import errors           # noqa: E402
from omegaforge.core.errors import _classify      # noqa: E402

# 磁盘/权限类文案：网络异常**绝不允许**落到这里。
# 判定用子串而非全等，这样改文案措辞不会误伤守卫。
DISK_PERMISSION_PHRASES = ("检查运行目录权限", "磁盘空间不足")


def _msg(exc: BaseException) -> str:
  return _classify(exc)[1]


def _code(exc: BaseException) -> str:
  return _classify(exc)[0]


# -- 1. 连接类异常必须走网络语义 ------------------------------------------

def _errno_of(*names):
  """取本平台的 errno 值，取不到就失败而不是跳过。

  写死 Linux 编号（113 / 101 / 104…）在 Windows 上指的是别的意思，于是
  用例测的其实是一个本地没有的编号：分类当然不匹配，红的是测试而不是产品。
  反过来，若一个平台上所有名字都取不到，说明这一层在该平台整个失效，
  必须报出来。
  """
  for n in names:
    v = getattr(errno, n, None)
    if v is not None:
      return v
  raise AssertionError(f"本平台缺少 {names} 对应的 errno，网络分支在那里无法成立")


NETWORK_CASES = [
  ("ConnectionResetError", lambda: ConnectionResetError(104, "Connection reset by peer")),
  ("RemoteDisconnected", lambda: http.client.RemoteDisconnected("Remote end closed")),
  ("ConnectionAbortedError", lambda: ConnectionAbortedError(103, "Software caused abort")),
  ("OSError(EHOSTUNREACH)", lambda: OSError(
    _errno_of("EHOSTUNREACH", "WSAEHOSTUNREACH"), "No route to host")),
  ("OSError(ENETUNREACH)", lambda: OSError(
    _errno_of("ENETUNREACH", "WSAENETUNREACH"), "Network is unreachable")),
  ("ContentTooShortError", lambda: urllib.error.ContentTooShortError("incomplete", None)),
]


def test_connection_errors_are_not_reported_as_disk_permission():
  """核心守卫：连接中断绝不能被说成磁盘权限问题。"""
  for name, factory in NETWORK_CASES:
    msg = _msg(factory())
    for bad in DISK_PERMISSION_PHRASES:
      assert bad not in msg, (
        f"{name} 被误报为磁盘问题：{msg!r}（用户会去查目录权限，"
        f"实际该检查网络与接口地址）"
      )
    assert msg, f"{name} 映射为空文案"


def test_connection_error_without_errno_is_not_disk_permission():
  """不带 errno 的连接异常：只有 ConnectionError 分支能接住。

  为什么必须单独这一条（回退校验验证得来的教训）：
   `ConnectionResetError(104, ...)` 同时是 **ConnectionError 和 OSError**
   的子类，而它的 errno=104 恰好在 `_NET_ERRNOS` 里。于是撤掉
   ConnectionError 分支后，它会被 OSError 的 errno 判定**接住**——
   回退校验显示守卫全绿，形同形式化。

  这与本项目反复出现的"两处互相兜底，单撤一处永远抓不到"是同一形态。
  所以必须有一个**只有 ConnectionError 分支能命中**的用例：
  errno=None 时 `_NET_ERRNOS` 判定不成立，撤掉即回落到磁盘权限兜底。
  """
  for name, exc in [
    ("ConnectionResetError(无errno)", ConnectionResetError("reset by peer")),
    ("ConnectionAbortedError(无errno)", ConnectionAbortedError("aborted")),
  ]:
    msg = _msg(exc)
    for bad in DISK_PERMISSION_PHRASES:
      assert bad not in msg, (
        f"{name} 失去 ConnectionError 分支后被误报为磁盘问题：{msg!r}"
      )


def test_ssl_family_is_never_reported_as_disk_permission():
  """SSL 全族必须走网络语义。

  验证（缺少该约束时，三个都落进「读写失败，请检查运行目录权限」）：

   ssl.CertificateError —— 白名单写的是 "CertificateError"，但 Python
    3.7+ 该类的实际 __name__ 是 **SSLCertVerificationError**
    （CertificateError 只是别名）→ 这个白名单项**从来没生效过**。
   ssl.SSLEOFError    —— SSLError 子类，名字不在白名单。
   （ssl.SSLError 本身反而在白名单里，所以是同族一半对一半错。）

  「写死的字符串与运行时类名对不上」与本项目处理过的
  「前端读的字段名后端根本没有」是同一类失效：读代码看不出来。
  """
  cases = [
    ("ssl.SSLError", ssl.SSLError("bad")),
    ("ssl.SSLEOFError", ssl.SSLEOFError("eof")),
    ("ssl.CertificateError", ssl.CertificateError("cert")),
  ]
  for name, exc in cases:
    msg = _msg(exc)
    for bad in DISK_PERMISSION_PHRASES:
      assert bad not in msg, f"{name} 被误报为磁盘问题：{msg!r}"
    assert _code(exc) == errors.CODE_NETWORK, f"{name} 错误码应为网络类"


def test_certificate_error_names_its_own_cause():
  """证书错误必须说"证书"，不能与握手失败混成一句。

  两者用户该做的动作不同：证书问题要去换/信任证书，握手失败可能是
  地址或中间设备。混成一句会误导。
  """
  msg = _msg(ssl.CertificateError("cert"))
  assert "证书" in msg, msg
  assert "证书" in _msg(ssl.SSLError("handshake"))


def test_ssl_subclasses_are_distinguishable_not_masked():
  """SSL 三个子类必须给出**不同**文案，不能都被父类分支接住。

  为什么必须有这一条（互相兜底，验证确认）：
  SSLCertVerificationError 与 SSLEOFError 都是 **SSLError 的子类**。
  于是撤掉它们各自的分支后，父类 `isinstance(exc, _ssl.SSLError)` 会
  把它们接住——文案仍是网络语义、错误码仍是 E_NETWORK，
  上面的 `test_ssl_family_is_never_reported_as_disk_permission`
  **照样全绿**，回退校验因此抓不到（守卫形同形式化）。

  这与本项目反复出现的「两处互相兜底，单撤一处永远抓不到」是同一形态，
  且这次发生在 isinstance 继承链上，比分散在两个文件里更隐蔽。
  所以必须断言三者文案**互不相同**且各自点名自己的语义。
  """
  cert = _msg(ssl.CertificateError("cert"))
  eof = _msg(ssl.SSLEOFError("eof"))
  plain = _msg(ssl.SSLError("handshake"))

  assert len({cert, eof, plain}) == 3, (
    f"SSL 三个子类文案发生合并，父类分支掩盖了子分支："
    f"cert={cert!r} eof={eof!r} plain={plain!r}")
  assert "证书" in cert and "中断" not in cert, cert
  assert "中断" in eof, eof
  assert "证书" in plain


def test_unlisted_ssl_subclasses_still_reach_network():
  """未列入白名单的 SSLError 子类，必须靠 isinstance 父分支接住。

  为什么单独这一条（回退校验验证得来）：
  裸 `ssl.SSLError` 同时被**上方类型名白名单**（含 "SSLError"）和
  **本次改动新增的 isinstance 父分支**接住——两处互相兜底，撤掉 isinstance
  分支后裸 SSLError 仍走网络语义，回退校验全绿、抓不到。

  该分支的真实价值不在裸 SSLError（白名单已覆盖），而在**名字不在白名单
  的其余子类**。验证 SSLError 共有 6 个子类，其中 4 个既不在白名单、
  也没有专门分支：

    SSLZeroReturnError / SSLSyscallError / SSLWantReadError /
    SSLWantWriteError

  撤掉父分支，这四个会穿透到 OSError 兜底，被说成磁盘权限问题。
  所以守卫必须落在它们身上，而不是落在裸 SSLError 身上。
  """
  for name, exc in [
    ("SSLZeroReturnError", ssl.SSLZeroReturnError("zero return")),
    ("SSLSyscallError", ssl.SSLSyscallError("syscall")),
    ("SSLWantReadError", ssl.SSLWantReadError("want read")),
    ("SSLWantWriteError", ssl.SSLWantWriteError("want write")),
  ]:
    code, msg = _classify(exc)
    assert code == errors.CODE_NETWORK, (
      f"{name} 未被 SSL 父分支接住，错误码 {code}")
    for bad in DISK_PERMISSION_PHRASES:
      assert bad not in msg, f"{name} 穿透到磁盘兜底：{msg!r}"


def test_herror_is_address_resolution_not_disk():
  """socket.herror 与 gaierror 同为地址解析失败。

  缺少该约束时：gaierror 在白名单里（对），herror 漏网 → 落到 OSError
  兜底被说成磁盘权限。又是"同族一半对一半错"。
  """
  code, msg = _classify(socket.herror(1, "host not found"))
  assert code == errors.CODE_NETWORK, code
  for bad in DISK_PERMISSION_PHRASES:
    assert bad not in msg, f"herror 被误报为磁盘问题：{msg!r}"


def test_non_disk_oserrors_fall_to_generic_internal():
  """ChildProcessError / ProcessLookupError / InterruptedError 不是磁盘问题。

  它们都是 OSError 子类，缺少该约束时落到"读写失败，请检查运行目录权限"——
  用户照着去查磁盘权限，而这些是子进程/信号层面的内部状态，重试无效。
  应走通用内部兜底（"操作失败，请稍后重试"）。
  """
  for name, exc in [
    ("ChildProcessError", ChildProcessError("no child")),
    ("ProcessLookupError", ProcessLookupError(3, "no process")),
    ("InterruptedError", InterruptedError(4, "interrupted")),
  ]:
    msg = _msg(exc)
    for bad in DISK_PERMISSION_PHRASES:
      assert bad not in msg, f"{name} 仍被说成磁盘问题：{msg!r}"
    assert msg == "操作失败，请稍后重试", f"{name} 应走通用兜底，实际 {msg!r}"


def test_bare_oserror_and_permission_stay_disk_semantics():
  """防处理过头：裸 OSError 与 PermissionError 必须保持磁盘语义。

  绝大多数 OSError 确实来自文件 IO，把兜底改成通用文案会让真正的文件
  问题失去可操作指引。这两条守的是"不要把修复做过头"。
  """
  assert _msg(OSError("boom")) == "读写失败，请检查运行目录权限"
  assert _msg(PermissionError("denied")) == "读写失败，请检查运行目录权限"


def test_incomplete_read_is_bad_response_not_internal():
  """IncompleteRead 是响应不完整，属于上游返回问题，不是内部故障。"""
  code, msg = _classify(http.client.IncompleteRead(b"abc"))
  assert code == errors.CODE_BAD_RESPONSE, (
    f"IncompleteRead 应归 E_BAD_RESPONSE，实际 {code} / {msg!r}"
  )
  for bad in DISK_PERMISSION_PHRASES:
    assert bad not in msg


def test_network_errnos_map_to_network_code():
  """OSError 携带网络 errno 时，不能落进磁盘权限兜底。"""
  import errno
  for name in ("ENETDOWN", "ENETUNREACH", "ENETRESET", "ECONNABORTED",
         "ECONNRESET", "EHOSTUNREACH", "ETIMEDOUT", "ECONNREFUSED"):
    no = getattr(errno, name, None)
    if no is None:            # 平台相关，缺失即跳过
      continue
    code, msg = _classify(OSError(no, name))
    assert code in (errors.CODE_NETWORK, errors.CODE_TIMEOUT), (
      f"OSError({name}) 应归网络类，实际 {code} / {msg!r}"
    )
    for bad in DISK_PERMISSION_PHRASES:
      assert bad not in msg


# -- 2. 防改动过头：磁盘类必须保持原样 --------------------------------------

def test_disk_errors_keep_their_own_message():
  """网络分支插在 OSError 之前，不能把磁盘类也一起截走。"""
  assert _msg(FileNotFoundError(2, "No such file")) == "未找到对应记录"
  assert _msg(IsADirectoryError(21, "Is a directory")) == "该路径是一个目录，请指定到文件"
  msg = _msg(NotADirectoryError(20, "Not a directory"))
  assert "文件而不是目录" in msg, msg
  assert _msg(FileExistsError(17, "exists")) == "该文件或目录已存在"
  assert _msg(PermissionError(13, "denied")) == "读写失败，请检查运行目录权限"
  assert _msg(BlockingIOError(11, "busy")) == "资源正被占用，请稍后重试"
  assert _msg(OSError(28, "No space left")) == "磁盘空间不足，请清理后重试"
  assert _msg(OSError(36, "too long")) == "路径过长，请改用更短的名称"


def test_broken_pipe_keeps_connection_message():
  """BrokenPipeError 是 ConnectionError 子类，但不能被网络分支改写。

  验证：网络分支若不加例外，BrokenPipeError 会从「连接已断开，请重试」
  变成「连接被中断或无法建立」——两者语义不同（写出端已关闭 vs 连不上）。
  """
  code, msg = _classify(BrokenPipeError(32, "Broken pipe"))
  assert msg == "连接已断开，请重试", f"BrokenPipeError 文案被网络分支改写：{msg!r}"
  assert code == errors.CODE_INTERNAL


def test_existing_network_whitelist_unchanged():
  """既有白名单成员不能因为新增 isinstance 分支而改文案。"""
  assert _msg(ConnectionRefusedError(111, "refused")) == "无法连接到模型服务，请检查网络与接口地址"
  assert _msg(TimeoutError("timed out")) == "请求超时，请检查网络后重试"
  assert _msg(socket.gaierror(-2, "Name not known")) == "无法连接到模型服务，请检查网络与接口地址"
  assert _msg(ssl.SSLError("bad handshake")) == "安全连接校验失败，请检查接口地址与证书"


# -- 3. 自定义异常未被网络分支截获 ----------------------------------------

def test_custom_exceptions_not_hijacked_by_network_branch():
  """网络分支插在自定义异常之前，必须确认没有把它们接走。

  CapabilityDenied 继承 PermissionError、BlockedCommand 等五个都是
  语义极强的专用分支，被网络分支截获等于用户看不到真正原因。
  """
  from omegaforge.tools.system_tools import (BlockedCommand,
                        CapabilityDenied,
                        McpScopeDenied)
  from omegaforge.tools.policy import ApprovalRequired, PlanRequired

  code, msg = _classify(BlockedCommand("该命令会删除系统文件"))
  assert code == errors.CODE_INVALID_INPUT and msg == "该命令会删除系统文件"

  code, msg = _classify(BlockedCommand("blocked"))
  assert msg == "该命令具有破坏性，已被安全策略拦截", msg

  code, msg = _classify(PlanRequired(tool="fs_write", plan={"tool": "fs_write"}))
  assert code == errors.CODE_PLAN, code

  code, msg = _classify(CapabilityDenied("web_fetch"))
  assert code == errors.CODE_PERMISSION and "访问网页需要授权" in msg

  code, msg = _classify(McpScopeDenied("fs.write"))
  assert code == errors.CODE_PERMISSION and "外部 MCP 客户端" in msg

  code, msg = _classify(ApprovalRequired("fs.write", "high", {}, "需要确认"))
  assert code == errors.CODE_APPROVAL, code

  code, msg = _classify(ApprovalRequired("fs.write", "high", {}, "需要确认", origin="mcp"))
  assert code == errors.CODE_APPROVAL and "无法弹出确认窗口" in msg


# -- 4. 端到端：真实 RST 连接 ----------------------------------------------

def test_real_connection_reset_speaks_network(tmp_path, monkeypatch):
  """起真 socket、发 RST，走完整 LLMClient 链路看用户最终看到什么。

  只测 `_classify` 不够——异常必须在**真实调用链**上产生并到达脱敏层，
  否则"接通断了"照样全绿（本项目已多次栽在这个形态上）。
  """
  import struct
  import threading
  monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))  # 绝不污染真实数据目录

  from omegaforge.llm.client import LLMClient

  srv = socket.socket()
  srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  srv.bind(("127.0.0.1", 0))
  port = srv.getsockname()[1]
  srv.listen(8)

  def serve():
    while True:
      try:
        c, _ = srv.accept()
      except OSError:
        return
      try:
        c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
               struct.pack("ii", 1, 0))
      except OSError:
        pass
      c.close()            # RST：不发任何响应

  threading.Thread(target=serve, daemon=True).start()

  cli = LLMClient(base_url=f"http://127.0.0.1:{port}/v1",
          api_key="sk-test-not-real")
  try:
    cli.chat("你是一个助手", "你好", model="m")
  except BaseException as e:
    msg = errors.user_error(e, context="test-reset")
  else:                  # pragma: no cover - 不该发生
    raise AssertionError("预期抛连接异常，却成功返回了")
  finally:
    srv.close()

  for bad in DISK_PERMISSION_PHRASES:
    assert bad not in msg, f"真实 RST 仍被报成磁盘问题：{msg!r}"
  assert "网络" in msg or "接口地址" in msg, f"未给出网络相关指引：{msg!r}"


# -- 5. 不变量：无英文/路径泄漏 --------------------------------------------

def test_no_internal_detail_leaks_for_network_errors():
  """脱敏底线：用户可见文案不得夹带英文异常名或系统路径。"""
  forbidden = ("ConnectionResetError", "RemoteDisconnected",
         "Traceback", "/usr/", "/tmp/", "errno")
  for name, factory in NETWORK_CASES:
    msg = _msg(factory())
    for bad in forbidden:
      assert bad not in msg, f"{name} 泄漏内部细节：{msg!r}"


def test_json_decode_error_still_bad_response():
  """JSONDecodeError 是 ValueError 子类，不能被网络分支抢走。"""
  code, _ = _classify(json.JSONDecodeError("expecting value", "doc", 0))
  assert code == errors.CODE_BAD_RESPONSE


# ---------------------------------------------------------------------------
# REVERT ANCHORS（回退校验用，勿删）
# ---------------------------------------------------------------------------
# A. 删除 _classify 中 `isinstance(exc, ConnectionError)` 分支
#  → test_connection_error_without_errno_is_not_disk_permission 变红
#  （注意：不能只靠带 errno 的用例——errno=104 在 _NET_ERRNOS 里，
#   会被 OSError 分支接住，形成互相掩盖。见该用例的 docstring。）
# B. 删除 `isinstance(exc, _http_client.HTTPException)` 分支
#  → test_incomplete_read_is_bad_response_not_internal 变红
# C. 删除 OSError 分支中的 `_NET_ERRNOS` 判定
#  → test_network_errnos_map_to_network_code 变红
# D. 删除 BrokenPipeError 例外（让网络分支截获）
#  → test_broken_pipe_keeps_connection_message 变红
#
# 脚本另有两个此处未列的校验点：
#   I. 同时删 A 与 F —— 端到端用例（真 socket 发 RST）只在两条一起撤时才红，
#      单撤任意一条都被另一条接住，故单锚点证明不了它有效。
#   H. 反向：把 PermissionError 也判成网络 —— 网络侧用例一条不红，
#      只有「磁盘类保持原样」那两条红。
#
# 已删除的无效校验点（如实记录，避免后来者重复踩）：
#  曾设 E「把网络分支上移到自定义异常之前，守护自定义异常映射」。
#  **不成立**——BlockedCommand / ApprovalRequired / PlanRequired /
#  CapabilityDenied / McpScopeDenied 没有一个是 ConnectionError 或
#  URLError 子类，网络分支根本不会截获它们。这是我臆想的风险，
#  不是真实风险，故不作为守卫目标（不为了凑校验点数而保留假守卫）。


def test_net_errno_set_is_not_empty():
  """网络 errno 集合在任何平台都不得为空。

  只按 POSIX 名字取 errno 的话，Windows 上这个集合会是空的：没有一条
  网络错误能命中，全部落进"读写失败，请检查运行目录权限"的兜底。空集合
  不报错，只是让这一层静默失效——所以必须有一条断言盯着它非空。
  """
  from omegaforge.core.errors import _NET_ERRNOS
  assert _NET_ERRNOS, "网络 errno 集合为空，网络类错误会被说成磁盘权限问题"
