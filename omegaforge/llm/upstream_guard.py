"""LLM 上游地址检查——模型接口地址（base_url）专用。

为什么不能直接用 tools.system_tools._guard_public_url
----------------------------------------------------
那个检查是给 web_fetch 用的，语义是"只能访问公网"，因此连 127.0.0.1
一起禁。而模型接口的头号使用场景恰恰是本机/局域网端点：Ollama
(127.0.0.1:11434)、LM Studio、vLLM，providers.py 里两个预设就是 local。
照搬等于把本地模型这条主线一起砍掉。

所以这里换一套口径：

    链路本地（169.254.0.0/16）、云元数据地址  →  硬禁
    回环、私有网段（10/8、192.168/16）        →  放行（本地模型要能用）

三条必须同时成立，缺一条就等于没守：

1. 初始 URL 要校验
2. **跳转目标要重新校验**——urllib 默认跟随 302，公网地址跳到
   169.254.169.254 就能绕过第 1 条
3. 响应体要封顶——上游返回超大内容会直接吃满内存，
   且与路由层的 8MB 口径保持一致
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..core.errors import UserError

# 单次响应体上限，与路由层 Content-Length 封顶同口径
MAX_UPSTREAM_BYTES = 8 * 1024 * 1024

# 云厂商元数据 / 凭据端点。访问这些地址等于把机器在云上的身份交出去，
# 是这条链上唯一"一次就致命"的场景，因此按字面域名硬禁。
CLOUD_METADATA_HOSTS = frozenset({
    "169.254.169.254",          # AWS / Azure / OpenStack / 阿里云
    "169.254.170.2",            # AWS ECS 任务元数据
    "100.100.100.200",          # 阿里云元数据
    "metadata.google.internal",  # GCP
    "metadata.goog",
    "metadata",
    "metadata.azure.com",
    "fd00:ec2::254",            # AWS IPv6 元数据
})


def _blocked_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # 只禁链路本地与未指定地址；私有网段与回环放行（本地模型）
    return bool(a.is_link_local or a.is_unspecified)


def guard_upstream_url(url: str) -> None:
    """校验模型接口地址，非法直接抛 UserError（中文、可行动）。"""
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
    except ValueError:
        raise UserError("接口地址无效，请检查后重试")
    if not host:
        raise UserError("接口地址无效，请检查后重试")
    if host in CLOUD_METADATA_HOSTS:
        raise UserError(
            "该接口地址指向云服务内部地址，已拒绝：继续访问会泄露本机的云上身份凭据")
    # 字面 IP
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        a = None
    if a is not None:
        if _blocked_ip(host):
            raise UserError("该接口地址指向云服务内部地址，已拒绝")
        return
    # 域名：解析后复查一遍，挡住"域名解析到元数据地址"的情况。
    # 说明：解析后校验存在 DNS rebinding 的理论窗口，本步不能替代网络层隔离。
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return
    for info in infos:
        ip = info[4][0]
        if _blocked_ip(ip):
            raise UserError(
                f"该接口地址解析到链路本地地址（{ip}），已拒绝")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """跳转目标重新过一遍检查——否则公网地址 302 到元数据即可绕过。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        guard_upstream_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirect)


def open_upstream(req: urllib.request.Request, timeout: int = 90,
                  max_bytes: int = MAX_UPSTREAM_BYTES) -> str:
    """发请求并取回响应体：跳转受控、体积封顶。

    超限抛 UserError 而不是静默截断——截断会让上层拿到一份"看起来完整"
    但其实是半截的 JSON，解析出的内容不可信。
    """
    guard_upstream_url(req.full_url)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
    except urllib.error.URLError as e:
        raise e
    if len(raw) > max_bytes:
        raise UserError(
            f"模型服务返回的内容过大（超过 {max_bytes // (1024 * 1024)}MB），"
            "请检查接口地址是否正确")
    return raw.decode("utf-8", "replace")
