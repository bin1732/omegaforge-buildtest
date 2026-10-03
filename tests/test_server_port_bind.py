"""启动路径的端口绑定守卫。

## 为什么需要这一层

后端由桌面外壳以后台进程方式启动，启动失败时界面上没有任何报错入口：
使用者看到的是"一直转圈"或"点了没反应"。此时启动路径的输出就是唯一
线索，它必须说清"下一步做什么"。

两条约束各自独立，缺一条都会让使用者在错误的方向上重试：

1. 绑定失败必须给出中文说明，且按 errno 分开处置——端口占用要"退出
   占用程序"，权限不足要"换个端口"，混成一句则两种场景都修不好。
2. 就绪声明必须晚于绑定成功。先声明后绑定时，使用者会照着一个并没有
   在监听的地址反复排查，而真正的线索只有一行未见异常的输出。

回退校验点（撤掉修复，下列用例必须变红）
------------------------------------------
  · serve 不捕获 OSError                    -> 1/2/3 变红
  · 把就绪打印挪回绑定之前                  -> 2 变红
  · sidecar 回退到直接回显原始异常          -> 5 变红
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omegaforge.core.errors import port_bind_error_text  # noqa: E402
from omegaforge.server import serve  # noqa: E402

# 固定高位端口：避开常用端口，且测试内先占住它，使绑定必然失败
PORT = 8799


class _Home:
    """隔离数据目录，避免守卫改动到使用者的真实数据。"""

    def setUp(self) -> None:
        self._snap = os.environ.get("OMEGAFORGE_HOME")
        os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_port_")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._snap is None:
            os.environ.pop("OMEGAFORGE_HOME", None)
        else:
            os.environ["OMEGAFORGE_HOME"] = self._snap


class PortBindTest(_Home, unittest.TestCase):
    def _occupy(self) -> socket.socket:
        """占住端口，使随后的绑定必然失败。

        这里**不能**设 SO_REUSEADDR：Windows 上该选项的语义与 Linux 不同
        ——它允许多次绑定同一地址端口（相当于 Linux 的 SO_REUSEPORT）。
        带上它，被观测的 bind 在该平台上会成功，serve_forever 随即进入
        永不返回的循环，用例从"失败"变成"挂起"。
        """
        s = socket.socket()
        s.bind(("127.0.0.1", PORT))
        s.listen(1)
        self.addCleanup(s.close)
        return s

    def _assert_occupied(self) -> None:
        """确认占用在本平台真的成立。

        占用若无效，被观测的绑定会成功，此后 serve 阻塞在事件循环里：
        症状是构建挂到超时，而不是用例失败——既拿不到失败项，也看不出
        是哪个用例。这里把前提不成立转成一条明确的失败。
        """
        probe = socket.socket()
        self.addCleanup(probe.close)
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as e:
            return
        probe.close()
        self.fail(f"端口 {PORT} 未被占住（本平台允许重复绑定：{e}），"
                  f"守卫前提不成立")

    def test_bind_failure_exits_with_chinese_text(self) -> None:
        """端口被占用：必须抛 SystemExit 且文案为中文、含端口号。"""
        self._occupy()
        self._assert_occupied()
        with self.assertRaises(SystemExit) as ctx:
            serve(host="127.0.0.1", port=PORT)
        text = str(ctx.exception)
        self.assertTrue(any("一" <= ch <= "龥" for ch in text),
                        f"启动说明必须是中文：{text!r}")
        self.assertIn(str(PORT), text, f"说明要点名端口：{text!r}")
        # 不得回显英文技术串
        for bad in ("Address already in use", "Errno", "OSError"):
            self.assertNotIn(bad, text, f"不得回显原始异常：{text!r}")

    def test_no_ready_message_when_bind_failed(self) -> None:
        """就绪声明不得早于绑定成功——失败时不能出现"已就绪"的输出。"""
        self._occupy()
        self._assert_occupied()
        buf: list[str] = []
        real_print = print

        def _spy(*args, **kwargs):
            buf.append(" ".join(str(a) for a in args))

        import builtins
        builtins.print = _spy                       # type: ignore[assignment]
        self.addCleanup(setattr, builtins, "print", real_print)
        try:
            serve(host="127.0.0.1", port=PORT)
        except SystemExit:
            pass
        joined = "\n".join(buf)
        self.assertNotIn("127.0.0.1", joined,
                         f"绑定失败时不得声明已就绪：{joined!r}")

    def test_ready_message_precedes_blocking(self) -> None:
        """就绪声明必须在进入阻塞循环之前发出。

        只验"绑定失败时不声明"拦不住反向的情形：声明被挪到阻塞之后，
        失败路径照样不打印，那条用例仍是绿的，而服务真正起来时使用者
        永远等不到就绪提示。所以这里用不会阻塞的替身记录调用次序。
        """
        import builtins

        import omegaforge.server as server_mod

        events: list[tuple[str, str]] = []

        class _FakeServer:
            def __init__(self, addr, handler):
                events.append(("bind", str(addr)))

            def serve_forever(self):
                events.append(("serve", ""))

        real_print = builtins.print
        real_cls = server_mod.ThreadingHTTPServer

        def _spy(*args, **kwargs):
            events.append(("print", " ".join(str(a) for a in args)))

        builtins.print = _spy                        # type: ignore[assignment]
        server_mod.ThreadingHTTPServer = _FakeServer  # type: ignore[assignment]
        try:
            server_mod.serve(host="127.0.0.1", port=PORT)
        finally:
            builtins.print = real_print              # type: ignore[assignment]
            server_mod.ThreadingHTTPServer = real_cls

        kinds = [k for k, _ in events]
        self.assertIn("bind", kinds, "必须先完成绑定")
        self.assertIn("serve", kinds, "必须进入服务循环")
        ready = [i for i, (k, t) in enumerate(events)
                 if k == "print" and "http://" in t]
        self.assertTrue(ready, f"未发出就绪声明：{events}")
        self.assertLess(ready[0], kinds.index("serve"),
                        f"就绪声明不得晚于阻塞：{events}")

    def test_default_port_falls_back_to_next_candidate(self) -> None:
        """默认端口被占用时退到候选端口的下一个，并说明改动。

        用户视角：上一次没退干净的残留进程占住默认端口，后端若直接退出，
        界面只会反复显示"无法连接到本机服务"，使用者既退不掉占用程序也换
        不了端口，应用等于不可用。用替身记录实际绑定的端口，避免真的进入
        阻塞循环——否则用例会从"失败"变成"挂起"。
        """
        import builtins

        import omegaforge.server as server_mod

        bound: list[int] = []
        printed: list[str] = []
        real_cls = server_mod.ThreadingHTTPServer
        real_watch = server_mod._start_uninstall_watch
        real_print = builtins.print

        class _Fake:
            def __init__(self, addr, handler):
                if addr[1] == server_mod.PORT_CANDIDATES[0]:
                    raise OSError(98, "Address already in use")
                bound.append(addr[1])

            def serve_forever(self):
                pass

        builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
        server_mod.ThreadingHTTPServer = _Fake         # type: ignore[assignment]
        server_mod._start_uninstall_watch = lambda _h: None  # type: ignore[assignment]
        self.addCleanup(setattr, builtins, "print", real_print)
        self.addCleanup(setattr, server_mod, "ThreadingHTTPServer", real_cls)
        self.addCleanup(setattr, server_mod, "_start_uninstall_watch", real_watch)
        try:
            server_mod.serve(host="127.0.0.1", port=server_mod.PORT_CANDIDATES[0])
        except SystemExit as exc:                       # pragma: no cover
            self.fail(f"默认端口被占用时应退到候选端口，实际退出：{exc}")

        self.assertEqual(bound, [server_mod.PORT_CANDIDATES[1]],
                         f"退让后必须落在下一个候选端口：{bound}")
        joined = "\n".join(printed)
        self.assertIn("被占用", joined, f"必须说明端口已被占用：{printed}")
        self.assertIn(str(server_mod.PORT_CANDIDATES[1]), joined,
                      f"必须点名实际使用的端口：{printed}")

    def test_explicit_port_is_not_silently_replaced(self) -> None:
        """显式指定的端口被占用时不得悄悄改用别的端口。

        否则使用者要求换端口却拿到另一个端口，报错点名的端口与实际监听的
        端口不一致，排查方向被带偏。
        """
        import builtins

        import omegaforge.server as server_mod

        real_cls = server_mod.ThreadingHTTPServer
        real_watch = server_mod._start_uninstall_watch
        real_print = builtins.print
        printed: list[str] = []

        class _AlwaysBusy:
            def __init__(self, addr, handler):
                raise OSError(98, "Address already in use")

            def serve_forever(self):
                pass

        builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
        server_mod.ThreadingHTTPServer = _AlwaysBusy    # type: ignore[assignment]
        server_mod._start_uninstall_watch = lambda _h: None  # type: ignore[assignment]
        self.addCleanup(setattr, builtins, "print", real_print)
        self.addCleanup(setattr, server_mod, "ThreadingHTTPServer", real_cls)
        self.addCleanup(setattr, server_mod, "_start_uninstall_watch", real_watch)
        with self.assertRaises(SystemExit):
            server_mod.serve(host="127.0.0.1", port=PORT)
        self.assertNotIn("127.0.0.1", "\n".join(printed),
                         f"未绑定成功时不得声明已就绪：{printed}")

    def test_error_text_separates_causes(self) -> None:
        """占用与权限不足必须给出不同的处置，且都点名端口。"""
        import errno as errno_mod

        busy = port_bind_error_text(PORT, OSError(errno_mod.EADDRINUSE, "busy"))
        denied = port_bind_error_text(PORT, OSError(errno_mod.EACCES, "denied"))
        self.assertNotEqual(busy, denied, "两种成因的处置必须不同")
        # 必须点名成因本身：只断言"两句不同"会被任意改写蒙过去
        self.assertIn("占用", busy, f"端口占用要说清成因：{busy!r}")
        self.assertIn("权限", denied, f"权限不足要说清成因：{denied!r}")
        self.assertIn(str(PORT), busy)
        self.assertIn(str(PORT), denied)
        self.assertIn("1024", denied, "权限不足要指引改用高位端口")

    def test_error_text_fallback_is_chinese(self) -> None:
        """无法归类的异常同样不得回显英文。"""
        text = port_bind_error_text(PORT, RuntimeError("boom"))
        self.assertTrue(any("一" <= ch <= "龥" for ch in text))
        self.assertNotIn("boom", text)

    def test_sidecar_uses_shared_text(self) -> None:
        """桌面端入口必须复用同一份文案，不得自行回显原始异常。"""
        path = os.path.join(ROOT, "scripts", "sidecar_main.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("SystemExit", src,
                      "桌面端入口必须承接启动路径给出的中文说明")
        self.assertNotIn("启动失败: {exc}", src,
                         "不得直接回显英文原始异常")


if __name__ == "__main__":
    unittest.main()
