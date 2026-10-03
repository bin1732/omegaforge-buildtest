"""工具层参数体积上限守卫。

本文件每一条都对应一个可复现过的问题。共同点：**上限只作用在"返回给
用户"的一侧，没有作用在"实际读取/写入"的一侧**。

1. **fs_read 的 max_bytes 只截断返回值，不约束读取量。**
  原实现 `p.read_bytes()[:max_bytes]`：先把整个文件读进内存，再切片丢弃。
  验证 30MB 文件、默认 max_bytes=65536，进程照样吃下 30MB；工作区里放一个
  1GB 日志，一次**默认** fs_read 就能把进程撑爆——不需要调用方传任何特殊
  参数。同时 `max_bytes=-1` 走负切片，返回"除最后一字节外的全部"；
  `None` 让切片不生效；`"100"` / `1.5` 直接 TypeError 冒泡成 500。

2. **fs_write 是唯一没有体量上限的写入口。**
  验证 40MB 内容一次写入成功。而 kb / memory 已有体量上限——同一份超长
  内容走 kb_add 被拒、走 fs_write 照写不误，双标会让用户以为"换个入口
  就能存下"。

3. **run_command 把进程全部输出读进内存。**
  验证 `head -c 200M /dev/zero` 峰值 584MB RSS，而返回给上下文只有 8000
  字符。默认 20 秒超时内足以把进程撑爆。

4. **web_fetch 的 SSRF 防护只校验初始 URL，不校验跳转目标。**
  验证公网域名 302 到 `http://127.0.0.1:<port>/latest/meta-data`（真实场景
  为 169.254.169.254）时 urllib 默认跟随，云元数据被原样读回——防护挡住了
  直连，没挡住跳转。

回退校验（见文件末尾）：撤掉任一处修复，对应守卫必须变红。
"""

from __future__ import annotations

import builtins
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import http.server
import socketserver
import unittest
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)


from omegaforge.tools import system_tools as st_mod     # noqa: E402
from omegaforge.tools.policy import Policy          # noqa: E402
from omegaforge.core.errors import UserError, user_error   # noqa: E402

CAP = st_mod.FS_READ_CAP
WRITE_CAP = st_mod.FS_WRITE_CAP
RETURN = st_mod.CMD_OUTPUT_RETURN


def _mk_home(name: str) -> str:
  home = tempfile.mkdtemp(prefix="tools_caps_%s_" % name)
  with open(os.path.join(home, "permissions.json"), "w") as f:
    json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)
  Policy(home).set_mode("full")
  return home


class _ReadSpy:
  """记录传给 file.read() 的 size 参数——用来证明"读取量"受约束。

  只断言返回值长度是不够的：返回值可以被截断而读取量不受控，
  那正是 fs_read 的原失效形态。
  """

  def __init__(self):
    self.sizes: list = []

  def __enter__(self):
    self._real = builtins.open
    spy = self

    def _open(file, mode="r", *a, **k):
      f = spy._real(file, mode, *a, **k)
      if "b" in mode and "w" not in mode and "a" not in mode:
        class _W:
          def read(self, n=-1):
            spy.sizes.append(n)
            return f.read(n)

          def __getattr__(self, k2):
            return getattr(f, k2)

          def __enter__(self):
            return self

          def __exit__(self, *x):
            f.close()
            return False
        return _W()
      return f

    builtins.open = _open
    return self

  def __exit__(self, *a):
    builtins.open = self._real
    return False


class FsReadCapTest(unittest.TestCase):
  def setUp(self):
    self.home = _mk_home("read")
    self.st = st_mod.SystemTools(self.home)
    self.big = os.path.join(self.home, "big.bin")
    with open(self.big, "wb") as f:
      f.write(b"A" * (4 * 1024 * 1024))   # 4MB

  def test_1_read_volume_is_bounded_by_max_bytes(self):
    """默认调用时传给 read() 的 size 必须 <= 默认上限。"""
    with _ReadSpy() as spy:
      r = self.st.fs_read("big.bin")
    self.assertTrue(spy.sizes, "未捕获到任何 read 调用")
    # 无界 read(-1) 正是内存炸弹的形态：不能只断言返回值长度，
    # 必须断言"读了多少"——read_bytes() 全读 + 切片丢弃同样满足长度断言。
    self.assertTrue(all(s > 0 for s in spy.sizes),
            "存在无界 read(-1/None)：读取量不随 max_bytes 收敛")
    self.assertLessEqual(max(spy.sizes), st_mod.FS_READ_DEFAULT,
               "读取量未被 max_bytes 约束——等于把整个文件读进内存")
    self.assertLessEqual(len(r["text"]), st_mod.FS_READ_DEFAULT)
    self.assertTrue(r["truncated"])

  def test_2_huge_max_bytes_is_capped(self):
    r = self.st.fs_read("big.bin", max_bytes=10 ** 9)
    self.assertLessEqual(len(r["text"]), CAP,
               "超大 max_bytes 未收敛到硬上限")
    self.assertLessEqual(len(r["text"]), 4 * 1024 * 1024)

  def test_3_negative_and_none_fall_back_to_default(self):
    """负数走负切片、None 让切片失效——两者都必须回落默认值。"""
    for bad in (-1, None):
      r = self.st.fs_read("big.bin", max_bytes=bad)
      self.assertEqual(len(r["text"]), st_mod.FS_READ_DEFAULT,
               "max_bytes=%r 未回落默认，读取量失控" % (bad,))

  def test_4_non_integer_does_not_raise(self):
    """'100' / 1.5 曾 TypeError 冒泡成 500「操作失败」。"""
    for bad in ("100", 1.5, [], {}):
      r = self.st.fs_read("big.bin", max_bytes=bad)
      self.assertLessEqual(len(r["text"]), CAP)

  def test_5_small_file_is_not_truncated(self):
    p = os.path.join(self.home, "small.txt")
    with open(p, "w", encoding="utf-8") as f:
      f.write("你好世界 hello\n第二行")
    r = self.st.fs_read("small.txt")
    self.assertFalse(r["truncated"])
    self.assertIn("第二行", r["text"])


class FsWriteCapTest(unittest.TestCase):
  def setUp(self):
    self.home = _mk_home("write")
    self.st = st_mod.SystemTools(self.home)

  def test_1_oversized_write_is_rejected(self):
    with self.assertRaises(UserError) as ctx:
      self.st.fs_write("huge.txt", "B" * (WRITE_CAP + 1024))
    self.assertIn("请拆分后再写入", str(ctx.exception))

  def test_2_message_reaches_the_surface(self):
    """抛 ValueError 会被 _classify 压成「请求内容有误」，中文到不了界面。"""
    try:
      self.st.fs_write("huge.txt", "B" * (WRITE_CAP + 1024))
    except UserError as e:
      self.assertEqual(user_error(e), "单次写入上限 %d KB，本次 %d KB，"
               "请拆分后再写入" % (WRITE_CAP // 1024,
                         (WRITE_CAP + 1024) // 1024))
    else:
      self.fail("超限写入未被拒绝")

  def test_3_at_cap_is_allowed(self):
    r = self.st.fs_write("edge.txt", "B" * (WRITE_CAP // 1024 * 1024))
    self.assertGreaterEqual(r["bytes"], 1024 * 1024 - 8)

  def test_4_normal_write_unaffected(self):
    r = self.st.fs_write("ok.md", "# 标题\n\n正文\n")
    self.assertEqual(r["path"], "ok.md")
    with open(os.path.join(self.home, "ok.md"), encoding="utf-8") as f:
      self.assertEqual(f.read(), "# 标题\n\n正文\n")


class CommandOutputCapTest(unittest.TestCase):
  def setUp(self):
    self.home = _mk_home("cmd")
    self.st = st_mod.SystemTools(self.home)

  def test_1_output_is_not_captured_into_memory(self):
    """capture_output=True 会把全部输出收进内存；必须改走临时文件。"""
    seen = {}

    def _spy(*a, **k):
      seen.update(k)
      return subprocess.CompletedProcess(a, 0, "", "")

    with mock.patch.object(st_mod.subprocess, "run", _spy):
      self.st.run_command("echo hi")
    self.assertNotIn("capture_output", seen,
             "仍在用 capture_output——输出量不受控")
    self.assertIn("stdout", seen, "输出应重定向到文件对象")

  def test_2_large_output_is_truncated_and_bounded(self):
    r = self.st.run_command("seq 1 200000 | tr -d '\\n'", timeout=20)
    self.assertLessEqual(len(r["output"]), RETURN)
    self.assertTrue(r["truncated"])

  def test_3_truncated_flag_uses_return_side_not_memory_cap(self):
    """19KB 输出会被截到 8000 字符；若按 64KB 判定会误标 False。"""
    r = self.st.run_command("seq 1 5000 | tr -d '\\n'", timeout=20)
    self.assertLessEqual(len(r["output"]), RETURN)
    self.assertTrue(r["truncated"],
            "truncated 按内存上限判定，用户会以为看到的是全部")

  def test_4_normal_output_unaffected(self):
    r = self.st.run_command("echo 你好")
    self.assertTrue(r["ok"])
    self.assertIn("你好", r["output"])
    self.assertFalse(r["truncated"])

  def test_5_timeout_still_works(self):
    r = self.st.run_command("sleep 5", timeout=1)
    self.assertFalse(r["ok"])
    self.assertIn("timeout", r["output"])


# ------------------------------------------------------------------ SSRF
class _RedirectTarget(http.server.BaseHTTPRequestHandler):
  """模拟云元数据 / 本机内网服务。"""

  BODY = b"CLOUD_METADATA_SECRET=AKIA-INTERNAL"

  def do_GET(self):
    self.send_response(200)
    self.send_header("Content-Type", "text/plain")
    self.send_header("Content-Length", str(len(self.BODY)))
    self.end_headers()
    self.wfile.write(self.BODY)

  def log_message(self, *a):
    pass


class _Redirector(http.server.BaseHTTPRequestHandler):
  """公网跳板：302 到内网目标。target 为类变量，测试运行时注入。"""

  target = ""
  hops = 0

  def do_GET(self):
    _Redirector.hops += 1
    loc = _Redirector.target
    if "{n}" in loc:
      loc = loc.format(n=_Redirector.hops)
    self.send_response(302)
    self.send_header("Location", loc)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def log_message(self, *a):
    pass


class WebFetchRedirectTest(unittest.TestCase):
  """SSRF：_guard_public_url 与 critical_check 都只校验初始 URL。"""

  @classmethod
  def setUpClass(cls):
    cls._real_ga = socket.getaddrinfo

    def _ga(host, port, *a, **k):
      if host == "fake-public.example":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("127.0.0.1", port or 0))]
      return cls._real_ga(host, port, *a, **k)

    socket.getaddrinfo = _ga
    cls._real_guard = st_mod._guard_public_url
    # 模拟"初始 URL 解析到公网 IP、已通过公网校验"
    st_mod._guard_public_url = lambda url: None

    cls.srv_target = socketserver.TCPServer(("127.0.0.1", 0), _RedirectTarget)
    cls.target_port = cls.srv_target.server_address[1]
    cls.srv_hop = socketserver.TCPServer(("127.0.0.1", 0), _Redirector)
    cls.hop_port = cls.srv_hop.server_address[1]
    threading.Thread(target=cls.srv_target.serve_forever, daemon=True).start()
    threading.Thread(target=cls.srv_hop.serve_forever, daemon=True).start()

  @classmethod
  def tearDownClass(cls):
    socket.getaddrinfo = cls._real_ga
    st_mod._guard_public_url = cls._real_guard
    cls.srv_target.shutdown()
    cls.srv_hop.shutdown()

  def setUp(self):
    self.home = _mk_home("ssrf")
    self.st = st_mod.SystemTools(self.home)
    _Redirector.hops = 0

  def _url(self, path="/start"):
    return "http://fake-public.example:%d%s" % (self.hop_port, path)

  def test_1_redirect_to_loopback_is_blocked(self):
    """公网地址跳转到本机元数据服务，必须拦下并说明拦的是哪个地址。

    断言落在"拦截 + 点名地址"上，而不是某一句固定措辞：文案措辞会
    随对外用语调整，断言若钉死措辞，文案改对之后反而会误报——那时
    报错指向的是测试，不是产品。
    """
    _Redirector.target = "http://127.0.0.1:%d/latest/meta-data" % self.target_port
    with self.assertRaises(st_mod.BlockedCommand) as ctx:
      self.st.web_fetch(self._url())
    msg = str(ctx.exception)
    self.assertIn("禁止访问", msg)
    self.assertIn("127.0.0.1", msg)

  def test_2_redirect_to_link_local_metadata_ip_is_blocked(self):
    """169.254.169.254 命中 critical_check 的主机名清单。"""
    _Redirector.target = "http://169.254.169.254/latest/meta-data"
    with self.assertRaises(st_mod.BlockedCommand):
      self.st.web_fetch(self._url())

  def test_3_non_http_redirect_is_blocked(self):
    """urllib 自带地拦下了 file://，但**允许** ftp —— 那正是要补的口子。

    验证：file:// 在进 redirect_request 之前就被 urllib 的 scheme 白名单
    拦掉，所以这条必须用 ftp:// 才能验到我们自己的守卫。
    """
    _Redirector.target = "ftp://fake-public.example/secret"
    with self.assertRaises(UserError) as ctx:
      self.st.web_fetch(self._url())
    self.assertIn("http(s)", str(ctx.exception))

  def test_4_redirect_loop_is_bounded(self):
    _Redirector.target = ("http://fake-public.example:%d/{n}"
               % self.hop_port)
    with self.assertRaises(Exception) as ctx:
      self.st.web_fetch(self._url())
    self.assertIn("跳转次数", str(ctx.exception))
    self.assertLessEqual(_Redirector.hops, st_mod.REDIRECT_MAX + 1,
               "跳数未被限制")

  def test_5_normal_redirect_still_works(self):
    """防处理过头：公网→公网的正常跳转不能被误伤。"""
    _Redirector.target = "http://fake-public.example:%d/final" % self.target_port
    r = self.st.web_fetch(self._url())
    self.assertIn("CLOUD_METADATA_SECRET", r["text"])


class _SpyResponse:
  """假响应：记录 read(n) 收到的 n——即"实际读取量上限"。

  返回值长度不足以证明这一点（返回侧会被截到 8000），必须看传给 read
  的参数。这也正是 fs_read 原失效的形态：返回值被截断，读取量不受控。
  """

  def __init__(self, body: bytes = b"x" * 4_000_000):
    self.body = body
    self.sizes: list = []
    self.headers = {"Content-Type": "text/plain"}

  def read(self, n=-1):
    self.sizes.append(n)
    return self.body[: n if isinstance(n, int) and n > 0 else len(self.body)]

  def __enter__(self):
    return self

  def __exit__(self, *a):
    return False


class WebFetchCapTest(unittest.TestCase):
  """web_fetch 的体积参数必须收敛，与 fs_read 同一口径。

  原实现把 max_bytes 直接交给 r.read()：None 等于读全部、-1 走负切片、
  "100"/1.5 抛 TypeError 冒泡成 500。
  """

  def setUp(self):
    self.home = _mk_home("wfetch")
    self.st = st_mod.SystemTools(self.home)
    self._real_guard = st_mod._guard_public_url
    st_mod._guard_public_url = lambda url: None

  def tearDown(self):
    st_mod._guard_public_url = self._real_guard

  def _fetch(self, max_bytes):
    spy = _SpyResponse()
    with mock.patch.object(urllib.request.OpenerDirector, "open",
                return_value=spy):
      self.st.web_fetch("http://fake-public.example/x",
               max_bytes=max_bytes)
    return spy

  def test_1_none_falls_back_to_default(self):
    """None 必须落到默认值，不能读全部 4MB。"""
    spy = self._fetch(None)
    self.assertEqual(spy.sizes, [st_mod.WEB_FETCH_DEFAULT])

  def test_2_negative_falls_back_to_default(self):
    """-1 是负切片，会返回"除最后一字节外的全部"，必须收敛。"""
    spy = self._fetch(-1)
    self.assertEqual(spy.sizes, [st_mod.WEB_FETCH_DEFAULT])

  def test_3_oversized_is_capped(self):
    """10**9 必须收敛到硬上限，不能按填的值去读。"""
    spy = self._fetch(10 ** 9)
    self.assertEqual(spy.sizes, [st_mod.WEB_FETCH_CAP])

  def test_4_non_integer_does_not_raise(self):
    """字符串/小数不能冒泡成 500「操作失败」。"""
    for bad in ("100", 1.5, object()):
      spy = self._fetch(bad)
      self.assertTrue(spy.sizes, "体积参数异常时应照常发起读取")
      self.assertLessEqual(spy.sizes[0], st_mod.WEB_FETCH_CAP)

  def test_5_normal_value_is_respected(self):
    """防处理过头：正常值不能被改成默认值。"""
    spy = self._fetch(4096)
    self.assertEqual(spy.sizes, [4096])


if __name__ == "__main__":
  unittest.main(verbosity=2)
