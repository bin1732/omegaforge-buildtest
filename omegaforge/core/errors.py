"""errors — 面向用户的错误转译层。

为什么需要这一层：

    直接把 Python 异常拼进用户可见响应时：
        ev({"type": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        chat_err = f"{type(e).__name__}: {str(e)[:100]}"
        self._json({"error": str(e)}, 400)

    这会把三类开发痕迹直接暴露到界面上：
      1. 异常类名         —— URLError / JSONDecodeError / KeyError，用户看不懂
      2. 内部消息         —— 英文原文、字段名、模块路径、URL、文件路径
      3. 可能的敏感信息   —— 请求 URL 里可能带 key、本地路径暴露目录结构

    本模块的做法：
      · 对外：只给人类可读的中文短句 + 一个稳定的错误码（便于排查，不泄内部）
      · 对内：完整异常（含类型、消息、traceback）写入运行日志，供开发者排查
      · 兜底：任何未识别异常一律映射为通用文案，绝不回显原始消息

    UserError 是唯一的例外通道：业务校验想让用户看到具体原因时，
    显式 raise UserError("…")，其中的文案被视为已审校、可安全展示。
"""

from __future__ import annotations

import errno as _errno
import http.client as _http_client
import logging
import os
import re

from .paths import resolve_home
import socket as _socket
import ssl as _ssl
import traceback
import urllib.error as _urlerror
from typing import Any

_log = logging.getLogger("omegaforge.errors")

# 错误码：稳定、可枚举，前端可据此做差异化提示，但不包含任何内部细节
CODE_NETWORK = "E_NETWORK"
CODE_TIMEOUT = "E_TIMEOUT"
CODE_AUTH = "E_AUTH"
CODE_FORBIDDEN = "E_FORBIDDEN"
CODE_NOT_FOUND = "E_NOT_FOUND"
CODE_RATE_LIMIT = "E_RATE_LIMIT"
CODE_UPSTREAM = "E_UPSTREAM"
CODE_BAD_RESPONSE = "E_BAD_RESPONSE"
CODE_INVALID_INPUT = "E_INVALID_INPUT"
CODE_INTERNAL = "E_INTERNAL"
CODE_PERMISSION = "E_PERMISSION"
CODE_APPROVAL = "E_APPROVAL"
# 计划模式：不执行，改为返回将要执行的计划（tools/policy.py 的 PlanRequired）。
# 状态值必须集中定义为常量：各处散写字面量，就无法校验前后端是否一致。
CODE_PLAN = "E_PLAN"

_DEFAULT_MSG = "操作失败，请稍后重试"

# 系统能力内部标识 → 用户可见名称（避免把 web_fetch 这类标识直接甩给用户）
_CAPABILITY_CN = {
    "terminal": "运行终端命令",
    "run_command": "运行终端命令",
    "fs_read": "读取本地文件",
    "fs_write": "写入本地文件",
    "fs_list": "查看本地目录",
    "web_fetch": "访问网页",
    "web": "访问网页",
}

# MCP 作用域（外部客户端）内部工具名 → 中文
_MCP_SCOPE_CN = {
    "terminal": "执行终端命令",
    "fs.write": "写入本地文件",
    "web_fetch": "访问网页",
}

# 网络类 errno：OSError 家族里属于「网络断了」而不是「磁盘读写失败」的那些。
#
# OSError 分支的兜底一律返回「读写失败，请检查运行目录权限」，
# 于是主机不可达、连接被重置、连接超时全都被说成**磁盘权限问题**——用户会
# 照着去查目录权限，而真正该做的是检查网络与接口地址。这类误导比"报错"更糟：
# 用户做了无效排查后，会以为不是网络问题。
_NET_ERRNO_NAMES = (
    "ENETDOWN", "ENETUNREACH", "ENETRESET", "ECONNABORTED", "ECONNRESET",
    "EHOSTUNREACH", "EHOSTDOWN", "ENOTCONN", "ETIMEDOUT", "ECONNREFUSED",
    "ESHUTDOWN",
)
# Windows 不提供上面这批 POSIX 名字，socket 层的错误带的是 WSA 编号
# （10050 段）。只按 POSIX 名字取，在 Windows 上这个集合会**整个为空**——
# 于是主机不可达、连接被重置全被兜底说成"读写失败，请检查运行目录权限"，
# 用户照着去查目录权限，而真正该查的是网络与接口地址。空集合不会报错，
# 只会让这一层静默失效，所以两组名字都要取。
_WSA_ERRNO_NAMES = (
    "WSAENETDOWN", "WSAENETUNREACH", "WSAENETRESET", "WSAECONNABORTED",
    "WSAECONNRESET", "WSAEHOSTUNREACH", "WSAEHOSTDOWN", "WSAENOTCONN",
    "WSAETIMEDOUT", "WSAECONNREFUSED", "WSAESHUTDOWN",
)
_NET_ERRNOS = frozenset(
    e for e in (
        *[getattr(_errno, n, None) for n in _NET_ERRNO_NAMES],
        *[getattr(_errno, n, None) for n in _WSA_ERRNO_NAMES],
    ) if e is not None
)

# 可能夹带密钥/令牌的字段，回显前必须抹掉
_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})", re.I),
    re.compile(r"(Bearer\s+[A-Za-z0-9._\-]{8,})", re.I),
    re.compile(r"((?:api[_-]?key|token|secret|password)[\"'\s:=]+[A-Za-z0-9._\-]{8,})", re.I),
]


class UserError(Exception):
    """业务校验异常：message 被视为已审校，可安全展示给用户。

    与 ValueError 的区别：ValueError 的文案是给开发者看的（常含英文与字段名），
    直接回显等于泄漏开发痕迹；UserError 是明确的"这句话就是给用户看的"。
    """

    def __init__(self, message: str, code: str = CODE_INVALID_INPUT):
        super().__init__(message)
        self.message = message
        self.code = code


def _is_displayable(msg: str) -> bool:
    """判断 message 是否像一句面向用户的中文短句。

    透传的前提是它确实能给人看：短于 6 字或不含中文的，多半是内部标识
    （如 "x"、"blocked"），直接回显等于把开发痕迹甩给用户，此时回落到
    通用拦截文案，守住"拦截必须说明原因"这条要求。
    """
    if not msg or len(msg) < 6:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in msg)


def _scrub(text: str) -> str:
    """抹掉文本中可能夹带的密钥片段——即使内容只进内部日志也先转译。"""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _ensure_handler() -> None:
    """确保 omegaforge logger 至少有一个真实处理器（惰性，仅出错时才跑）。

    为什么必须有这条兜底（，非理论推演）：
    logging 在整条链路上找不到任何 handler 时，会走内置的 lastResort
    handler 把记录**直接打到 stderr**。CLI 作为第二个入口从未调用过
    configure_logging，于是 user_error() 里"只进内部日志"的完整
    traceback 连同本机绝对路径出现在用户终端上——转译层在服务端成立，
    换到 CLI 就整体失效。

    与其要求每个入口都记得调用 configure_logging（已经漏过一次），
    不如让转译层自己兜底：**不靠调用方自觉**的构造式保证。
    落盘失败时退 NullHandler —— 宁可丢日志，也不能把栈吐给用户。
    """
    root = logging.getLogger("omegaforge")
    if getattr(root, "_of_handler_ready", False):
        return
    for h in root.handlers:
        if not isinstance(h, logging.NullHandler):
            root._of_handler_ready = True
            return
    try:
        configure_logging()
    except Exception:  # noqa: BLE001
        pass
    if not any(not isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())
    root._of_handler_ready = True


def _log_internal(exc: BaseException, context: str | None) -> None:
    """完整异常只进内部日志，不进任何用户可见通道。"""
    try:
        _ensure_handler()
        detail = _scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        _log.error(
            "user-visible-error-suppressed context=%s type=%s detail=%s",
            context or "-", type(exc).__name__, detail[-2000:],
        )
    except Exception:  # noqa: BLE001 — 日志本身绝不能影响主流程
        pass


def _http_status(exc: BaseException) -> tuple[str, str]:
    """按 HTTP 状态码映射（401/403/404/429/5xx 各自对应不同处置动作）。

    抽成函数是因为调用点有两处：urllib 走 isinstance、httpx 走类型名。
    分两处写必然出现"改了一处忘了另一处"——本项目已多次栽在这个形态上。
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 0
    if code == 401:
        return CODE_AUTH, "API 密钥无效或已过期"
    if code == 403:
        return CODE_FORBIDDEN, "当前密钥无权访问该模型"
    if code == 404:
        return CODE_NOT_FOUND, "接口地址或模型不存在"
    if code == 429:
        return CODE_RATE_LIMIT, "请求过于频繁，已被限流，请稍后重试"
    if 500 <= code < 600:
        return CODE_UPSTREAM, "模型服务暂时不可用，请稍后重试"
    return CODE_BAD_RESPONSE, "模型服务返回了异常响应"


def _classify(exc: BaseException) -> tuple[str, str]:
    """把 Python 异常映射为 (错误码, 中文文案)。

    按类型名匹配而非 import 具体类，避免为 httpx/requests/urllib 各写一份分支。
    """
    name = type(exc).__name__

    # ---- 网络层 ----
    if name in {"TimeoutError", "socket.timeout", "ConnectTimeout", "ReadTimeout"}:
        return CODE_TIMEOUT, "请求超时，请检查网络后重试"

    if name in {"URLError", "ConnectionError", "ConnectionRefusedError",
                "ConnectError", "NewConnectionError", "gaierror"}:
        return CODE_NETWORK, "无法连接到模型服务，请检查网络与接口地址"

    if name in {"SSLError", "CertificateError"}:
        return CODE_NETWORK, "安全连接校验失败，请检查接口地址与证书"

    # ---- HTTP 状态：**必须排在 URLError 的 isinstance 兜底之前** ----
    # `urllib.error.HTTPError` 是 `URLError` 的**子类**，而 URLError 的
    # isinstance 分支写在下面几十行处。若本分支排在它之后，
    # 401/403/404/429/5xx 会被 URLError 分支截获，统一退化成
    # 「网络请求异常，请检查网络与接口地址」。
    #
    # 后果最重的一档是 401：密钥无效被说成网络不通，使用者会照着去查地址
    # 与网络，而改地址、换网络、重试多少次都不会成功。
    # 429 同理：限流被说成网络异常，重试只会更狠地撞限流。
    #
    # 正确的映射应为：
    #   401 -> E_AUTH「API 密钥无效或已过期」
    #   429 -> E_RATE_LIMIT「请求过于频繁…」
    #   500 -> E_UPSTREAM「模型服务暂时不可用」
    if isinstance(exc, _urlerror.HTTPError):
        return _http_status(exc)

    # ---- 网络层（isinstance 兜底）----
    # 上面三处全部按**类型名精确匹配**，于是同族中名字不在
    # 白名单的成员全部穿透到函数末尾或 OSError 分支：
    #
    #   ConnectionResetError    -> 读写失败，请检查运行目录权限
    #   RemoteDisconnected      -> 读写失败，请检查运行目录权限
    #   ConnectionAbortedError  -> 读写失败，请检查运行目录权限
    #   OSError(EHOSTUNREACH)   -> 读写失败，请检查运行目录权限
    #   ContentTooShortError    -> 读写失败，请检查运行目录权限
    #   IncompleteRead          -> 操作失败，请稍后重试
    #
    # 六种都是**连接被中断 / 主机不可达 / 响应不完整**，用户当场能做的是检查
    # 网络与接口地址，却被引导去查磁盘权限——重试一百次也不会成功。
    #
    # 这与 OSError 家族当年的白名单 bug 完全同源：按名字匹配，同一条继承链上
    # 一半对一半错（`ConnectionRefusedError` 在白名单里、`ConnectionResetError`
    # 不在），靠读代码极难发现，必须枚举。
    #
    # 注意这里的 isinstance 判定刻意**只覆盖网络语义**：httpx / requests 未安装
    # 时无法 isinstance 其具体类，所以类型名白名单仍保留在前面，两者互补而非替代。
    # BrokenPipeError 在 Python 继承链上是 ConnectionError 的子类，但语义是
    # 「写出端已关闭」而不是「网络不可达」。若让下面的 ConnectionError 分支
    # 先截获，它会从「连接已断开，请重试」变成「连接被中断或无法建立」——
    # 确认过。所以必须在这里单独放行，让下方 OSError 分支继续生效。
    if isinstance(exc, BrokenPipeError):
        return CODE_INTERNAL, "连接已断开，请重试"
    if isinstance(exc, TimeoutError):
        return CODE_TIMEOUT, "请求超时，请检查网络后重试"
    if isinstance(exc, ConnectionError):
        return CODE_NETWORK, "连接被中断或无法建立，请检查网络与接口地址"
    if isinstance(exc, _urlerror.URLError):
        return CODE_NETWORK, "网络请求异常，请检查网络与接口地址"
    # SSL 全族：白名单里写的是 `"CertificateError"`，但 Python 3.7+ 该类
    # 的实际 __name__ 是 **SSLCertVerificationError**（CertificateError 只是
    # 它的别名），字符串**从来匹配不上**——确认。而 SSLEOFError 是
    # SSLError 子类、名字同样不在白名单，于是整族从这处漏到 OSError 兜底，
    # 被说成「读写失败，请检查运行目录权限」。
    #
    # 这跟本项目反复出现的形态完全一致：按类型名匹配，同一条继承链上一半
    # 对一半错（SSLError 在白名单里、SSLEOFError 不在）。改用 isinstance
    # 覆盖整族。三层细分是有意义的：证书校验失败 / 连接被中断 / 其他握手问题，
    # 用户该做的动作不同，混成一句会误导。
    if isinstance(exc, _ssl.SSLCertVerificationError):
        return CODE_NETWORK, "安全证书校验失败，请检查接口地址与证书"
    if isinstance(exc, _ssl.SSLEOFError):
        return CODE_NETWORK, "安全连接被中断，请检查网络与接口地址"
    if isinstance(exc, _ssl.SSLError):
        return CODE_NETWORK, "安全连接校验失败，请检查接口地址与证书"
    # herror 与 gaierror 同为**地址解析**失败（gethostbyname 系列），但只有
    # gaierror 在上方白名单里。例如 herror 漏网后落到 OSError 兜底，被说成
    # 磁盘权限问题——同样是同族一半对一半错。
    if isinstance(exc, _socket.herror):
        return CODE_NETWORK, "无法解析主机地址，请检查网络与接口地址"
    if isinstance(exc, _http_client.HTTPException):
        return CODE_BAD_RESPONSE, "模型服务返回了不完整的响应"

    # ---- HTTP 状态 ----
    # urllib 的 HTTPError 已在上方按 isinstance 处理过了；这里保留按名字匹配，
    # 覆盖 httpx / aiohttp 的 HTTPStatusError、ClientResponseError——它们不是
    # URLError 子类，装了才有，不能用 isinstance 判。两者互补而非冗余。
    if name in {"HTTPError", "HTTPStatusError", "ClientResponseError"}:
        return _http_status(exc)

    # ---- 解析层 ----
    # JSONDecodeError 是 ValueError 子类，但类名不同，会先命中这里
    if name in {"JSONDecodeError", "DecodeError"}:
        return CODE_BAD_RESPONSE, "模型返回了无法解析的内容"

    # 走到这里的 ValueError 属于未预期的内部校验失败。
    # 业务校验应显式抛 UserError（已审校文案），不应依赖此处回显——
    # 否则英文原文与字段名会直接泄漏到界面。
    if name == "ValueError":
        return CODE_INVALID_INPUT, "请求内容有误，请检查后重试"

    if name in {"KeyError", "IndexError", "TypeError", "AttributeError"}:
        return CODE_INTERNAL, _DEFAULT_MSG

    # ---- 文件系统 ----
    if name == "FileNotFoundError":
        return CODE_NOT_FOUND, "未找到对应记录"
    if name == "BlockedCommand":
        # 安全拦截共用这一个异常类，但三种场景的 message 完全不同：
        #   破坏性命令 / 内网与云元数据地址 / 违反安全规则。
        # 因此不能一律压成"该命令具有破坏性"：那会把"这个网址不允许访问"
        # 说成"你这条命令很危险"，调用方（人或模型）据此以为"换条命令就行"，
        # 实际换任何命令都同样被拒，属于确定性失败。
        # 这些 message 都是代码里写死的中文短句，可安全展示。
        msg = str(exc).strip()
        if _is_displayable(msg):
            return CODE_INVALID_INPUT, _scrub(msg)[:120]
        return CODE_INVALID_INPUT, "该命令具有破坏性，已被安全策略拦截"
    if name == "ApprovalRequired":
        # MCP 客户端**没有弹窗确认这条通道**：ask 对它等于永远拒绝。
        # 而"该操作需要你确认后才会执行"会让人（和外部模型）以为等一等
        # 或者换个参数就行——这条路是确定性失败，重试多少次都一样。
        # 带 origin 的必须给出真正走得下去的指引：调高级别，或回界面操作。
        if getattr(exc, "origin", None) == "mcp":
            return CODE_APPROVAL, (
                "外部 MCP 客户端无法弹出确认窗口：当前权限级别下该操作"
                "需要本机确认，请在「设置 → 权限级别」调高后重试")
        return CODE_APPROVAL, "该操作需要你确认后才会执行"
    if name == "PlanRequired":
        # 计划模式：不是错误，是"先出计划再执行"。所有入口都必须处理这个
        # 异常并返回计划状态：一旦有入口漏掉，就会把"请先确认计划"说成
        # 服务器故障，调用方据此反复重试，而重试多少次结果都一样。
        plan = getattr(exc, "plan", None) or {}
        tool = str(plan.get("tool", "")).strip()
        return CODE_PLAN, (
            "计划模式：该操作不会立即执行，请先确认计划"
            + (f"（{tool}）" if tool else ""))
    if name == "CapabilityDenied":
        # 系统能力未授权 ≠ 文件系统权限错误，两者都叫 PermissionError 但语义不同，
        # 混用会让用户被引导去查磁盘权限（确认）。
        # 异常自带能力名（如 web_fetch），翻译成中文才不会把内部标识甩给用户。
        cap = _CAPABILITY_CN.get(str(exc).strip(), "该功能")
        return CODE_PERMISSION, f"{cap}需要授权，请在「设置 → 系统能力」中开启后重试"

    # ---- 本仓自定义、但**用户当场能改**的两类 ----
    # 这两类不能落到函数末尾的通用兜底：它们的内部说明是英文串，不能直接
    # 展示；但这两件事恰恰都是用户改一个数值就能继续的，说成服务器故障会
    # 让人反复重试——而重试必然再次失败（额度不会自己变多）。
    #
    # 用 isinstance 而不是类型名：两者都是 RuntimeError 子类，按名字匹配的话
    # 将来任何子类/改名都会静默退回兜底（本项目已多次栽在按名匹配上）。
    # 局部导入：budget / atomicio 都不依赖本模块，但反过来不成立，放在函数内
    # 可彻底避开循环导入。
    try:
        from ..core.budget import BudgetExceeded as _BudgetExceeded
        from ..core.atomicio import LockTimeout as _LockTimeout
    except Exception:                                   # noqa: BLE001
        _BudgetExceeded = _LockTimeout = ()
    if _BudgetExceeded and isinstance(exc, _BudgetExceeded):
        return CODE_INVALID_INPUT, "当前 token 预算已用尽，请调高预算后重试"
    if _LockTimeout and isinstance(exc, _LockTimeout):
        return CODE_INTERNAL, "另一处进程正在操作同一份数据，请稍后重试"

    if name == "McpScopeDenied":
        # 外部 MCP 客户端越出作用域。不能复用 CapabilityDenied 的文案：
        # 后者会引导调用方去开"系统能力"——那是本机的开关，一旦打开，
        # 所有挂载的 MCP host 立刻获得同等权限（确认）。
        tool = _MCP_SCOPE_CN.get(str(exc).strip(), "该操作")
        return CODE_PERMISSION, (
            f"外部 MCP 客户端默认不继承本机的{tool}权限，"
            f"请在「设置 → MCP 作用域」中单独开启后重试")

    # ---- 文件系统（按 isinstance 判定，不按类型名白名单）----
    # 这里写的是 `name in {"PermissionError", "OSError"}`，
    # 按**类型名精确匹配**。而 OSError 有十几个子类，白名单只列了两个，
    # 于是同族的其余成员全部穿透到函数末尾的兜底 ——
    #
    #   NotADirectoryError  → 操作失败，请稍后重试   （--out 填成了文件）
    #   IsADirectoryError   → 操作失败，请稍后重试   （把目录当文件读）
    #   FileExistsError     → 操作失败，请稍后重试
    #   BlockingIOError     → 操作失败，请稍后重试
    #   BrokenPipeError     → 操作失败，请稍后重试
    #
    # 五种全都是**填错了路径**这类用户当场就能改的情形，却被统一说成
    # 服务器故障——重试一百次也一样。而 PermissionError 恰好在白名单里，
    # 所以同一个继承链上的成员一半对一半错，最难靠肉眼发现。
    #
    # 按 isinstance 从最具体到最宽泛判定，新出现的 OSError 子类自动落进
    # 兜底分支而不再伪装成 500。
    if isinstance(exc, OSError):
        # 网络 errno 优先于"磁盘读写失败"兜底：同样是 OSError，主机不可达与
        # 权限不足是完全不同的两件事，不能共用一句文案。
        if getattr(exc, "errno", None) in _NET_ERRNOS:
            return CODE_NETWORK, "网络不可达或连接被中断，请检查网络与接口地址"
        if isinstance(exc, IsADirectoryError):
            return CODE_INVALID_INPUT, "该路径是一个目录，请指定到文件"
        if isinstance(exc, NotADirectoryError):
            return CODE_INVALID_INPUT, (
                "路径中有一项是文件而不是目录，请检查路径是否填写正确")
        if isinstance(exc, FileExistsError):
            return CODE_INVALID_INPUT, "该文件或目录已存在"
        if isinstance(exc, PermissionError):
            return CODE_INTERNAL, "读写失败，请检查运行目录权限"
        if isinstance(exc, BlockingIOError):
            return CODE_INTERNAL, "资源正被占用，请稍后重试"
        # 这三个是 OSError 子类，但**既不是磁盘也不是网络**：子进程不存在 /
        # 查不到进程 / 系统调用被信号中断。落到下方"读写失败，请检查运行目录
        # 权限"会把内部状态说成磁盘故障——用户照着去查权限，同样重试无效。
        # 它们该走的是通用内部兜底，而不是磁盘语义。
        if isinstance(exc, (ChildProcessError, ProcessLookupError,
                            InterruptedError)):
            return CODE_INTERNAL, _DEFAULT_MSG
        # BrokenPipeError 刻意不在这里重复处理：它在上方网络层已单独放行。
        # 保留两处会形成相互遮蔽——撤掉其中任意一处，另一处仍把它接住，
        # 回退验证因此永远抓不到（本项目已多次栽在这个形态上）。
        if getattr(exc, "errno", None) == 28:      # ENOSPC
            return CODE_INTERNAL, "磁盘空间不足，请清理后重试"
        if getattr(exc, "errno", None) == 36:      # ENAMETOOLONG
            return CODE_INVALID_INPUT, "路径过长，请改用更短的名称"
        return CODE_INTERNAL, "读写失败，请检查运行目录权限"

    return CODE_INTERNAL, _DEFAULT_MSG


def record_internal_error(exc: BaseException, context: str | None = None) -> None:
    """把完整调用栈记进内部日志，供排查。

    为什么单独开一个入口：未预期的编程错误在命令行里会一路抛到栈顶，
    Python 默认把英文调用栈连同本机绝对路径打到标准错误输出——那是使用
    者看得见的通道。若不在这里落盘，栈既给了使用者看，又没留下任何排查
    线索，两头落空。

    只记日志、不改文案：调用方自行决定给使用者看什么。
    """
    _log_internal(exc, context)


def user_error(exc: BaseException, context: str | None = None) -> str:
    """把任意异常转成可安全展示给用户的中文短句。

    UserError 原样透出（已审校）；其余一律走转译映射，
    并在内部日志留下完整 traceback 供排查。
    """
    if isinstance(exc, UserError):
        return exc.message

    code, msg = _classify(exc)
    _log_internal(exc, context)
    return msg


def user_error_payload(exc: BaseException, context: str | None = None) -> dict[str, Any]:
    """返回 {"error": 文案, "code": 错误码}，供 API 响应使用。"""
    if isinstance(exc, UserError):
        return {"error": exc.message, "code": exc.code}
    code, msg = _classify(exc)
    _log_internal(exc, context)
    return {"error": msg, "code": code}


def port_bind_error_text(port: int, exc: BaseException) -> str:
    """端口绑定失败时给使用者的说明。

    启动路径上的输出是使用者能拿到的全部线索：桌面端由外壳托管后端
    进程，这里的文字不会出现在界面上，因此必须说清"下一步做什么"，
    而不能回显原始异常——那是英文技术串，照着它无从下手。

    按 errno 分类：端口占用与权限不足的处置方式完全不同，混成一句
    会让使用者在错误的方向上重试。
    """
    if isinstance(exc, OSError):
        if exc.errno == _errno.EADDRINUSE:
            return (f"服务端口 {port} 已被其他程序占用，"
                    f"请先退出占用该端口的程序后重新启动。")
        if exc.errno == _errno.EACCES:
            return f"没有权限监听端口 {port}，请改用 1024 以上的端口后重试。"
    return f"无法在端口 {port} 启动服务，请更换端口后重试。"


def default_log_dir() -> str:
    """内部日志目录 —— 与 RunStore 同一套口径（OMEGAFORGE_HOME）。

    日志目录必须跟随统一数据目录：若写死路径而忽略环境变量，
    业务数据（runs/tasks/kb）会统一到数据目录，唯独落着完整调用栈的
    错误日志仍留在**当前工作目录**——桌面端运行目录不可控，
    既污染用户目录，也让"去数据目录找日志"的排查路径失效。

    这里必须复用 paths.resolve_home，不得就地再算一遍：打包运行时的兜底
    是用户目录，而就地算只会得到当前工作目录——桌面端由外壳拉起，工作目
    录落在安装目录，于是错误日志写进安装目录。该目录不在卸载清单里，卸载
    后永久留下，成为无人知晓的孤儿副本。

    OSError 一律降级为内存目录：日志目录不可写时**绝不阻断启动**。
    这是入口级失败：日志写不了最多没法排查，不该让整个应用起不来。
    """
    home = resolve_home()
    return os.path.join(home, "logs")


def configure_logging(log_dir: str | None = None) -> str:
    """把内部日志接到文件。默认写到 OMEGAFORGE_HOME/logs/errors.log。

    注意：这是给开发者排查用的，界面上不展示、API 不返回。
    **任何失败都不得抛出** —— 见 default_log_dir 的说明。

    返回实际使用的日志文件路径（失败时返回空串）。
    """
    log_dir = log_dir or default_log_dir()
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "errors.log")
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        # 目录建不了或文件开不了：降级为不落盘，仅保留调用方的既有行为。
        # 不向上抛——日志是旁路，不该变成启动失败的原因。
        return ""
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("omegaforge")
    if not any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(path)
               for h in root.handlers):
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    return path
