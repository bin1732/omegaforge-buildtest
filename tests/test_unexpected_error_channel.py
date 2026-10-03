"""未预期异常的输出通道守卫。

## 为什么需要这一层

入口外壳收口的都是可预期的失败。未预期的编程错误（例如配置结构不符合
约定而抛出的 TypeError）不在其中，一旦发生就一路抛到栈顶，由 Python
默认处理：

    Traceback (most recent call last):
      File "/…/omegaforge/cli.py", line 193, in main
    TypeError: …

两个后果同时成立：

1. 英文调用栈连同本机绝对路径出现在标准错误输出——这是使用者看得见的
   通道；
2. 内部日志里什么都没留下——排查线索反而丢了。

即"栈给了使用者看，却没留给排查"，两头落空。

## 约束

  · 使用者通道只出现中文，且不含英文异常名、不含绝对路径
  · 同一时刻，内部日志必须拿到完整调用栈

第二条是第一条成立的前提：若只改文案而不落盘，等于用掩盖换干净，
真实缺陷会彻底消失在视野里。

## 回退校验点（撤掉修复，对应用例必须变红）

  · 去掉 cli.main 的 Exception 分支      -> 1/2/3 变红
  · 去掉 record_internal_error 调用      -> 2/3 变红
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_cli_with_unexpected_error(home: str) -> tuple[int, str, str]:
    """在子进程里让命令行的业务层抛出未预期异常，返回 (码, 出, 错)。"""
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from omegaforge.cli import main
        import omegaforge.cli as c

        def boom(argv):
            raise TypeError("配置结构不符合预期")

        c._dispatch = boom
        sys.exit(main(["status"]))
    """)
    env = dict(os.environ, OMEGAFORGE_HOME=home,
               PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True, env=env,
                          timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def _run_sidecar_with_unexpected_error(home: str) -> tuple[int, str, str]:
    """让桌面端后台进程的服务入口抛出未预期异常，返回 (码, 出, 错)。"""
    code = textwrap.dedent(f"""
        import importlib.util, io, sys, contextlib
        sys.path.insert(0, {str(ROOT)!r})
        import omegaforge.server as srv

        def boom(*a, **k):
            raise RuntimeError("未预期的内部状态")

        srv.serve = boom
        spec = importlib.util.spec_from_file_location(
            "sidecar_main", {str(ROOT)!r} + "/scripts/sidecar_main.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.exit(mod.main())
    """)
    env = dict(os.environ, OMEGAFORGE_HOME=home, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True, env=env,
                          timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


class UnexpectedErrorChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="of_chan_")

    def _log_text(self) -> str:
        path = Path(self.home) / "logs" / "errors.log"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def test_user_channel_is_chinese_without_traceback(self) -> None:
        """使用者通道：中文一句，不得出现英文异常名与调用栈。"""
        code, _out, err = _run_cli_with_unexpected_error(self.home)
        self.assertNotEqual(code, 0, "未预期错误应以非零码退出")
        combined = (_out + err).strip()
        self.assertTrue(any("一" <= ch <= "龥" for ch in combined),
                        f"必须是中文提示：{combined!r}")
        for bad in ("Traceback", "TypeError", ".py", "line "):
            self.assertNotIn(bad, combined,
                             f"不得出现英文调用栈成分 {bad!r}：{combined!r}")

    def test_no_absolute_path_in_user_channel(self) -> None:
        """使用者通道不得回显本机绝对路径。"""
        _code, out, err = _run_cli_with_unexpected_error(self.home)
        for text in (out, err):
            for token in ("/data/", "/home/", str(ROOT)):
                self.assertNotIn(token, text,
                                 f"不得回显本机路径：{text!r}")

    def test_stack_is_recorded_for_diagnosis(self) -> None:
        """排查通道：完整调用栈必须落进内部日志。"""
        _run_cli_with_unexpected_error(self.home)
        text = self._log_text()
        self.assertIn("TypeError", text,
                      f"日志必须留下异常类型：{text[:200]!r}")
        self.assertIn("配置结构不符合预期", text,
                      f"日志必须留下异常原文：{text[:200]!r}")

    def test_sidecar_channel_is_clean_and_logged(self) -> None:
        """桌面端后台进程：同样只给中文，栈同样落盘。

        它的标准输出由外壳转发进日志面板，属于他人可见的通道，
        不能与命令行两套标准。
        """
        code, out, err = _run_sidecar_with_unexpected_error(self.home)
        self.assertEqual(code, 1, "未预期错误应以非零码退出")
        text = (out + err)
        for bad in ("Traceback", "RuntimeError"):
            self.assertNotIn(bad, text, f"不得回显英文调用栈：{text!r}")
        self.assertIn("errors.log", text, f"应指明日志位置：{text!r}")
        self.assertIn("RuntimeError", self._log_text(),
                      "后台进程同样要把栈记进日志")


if __name__ == "__main__":
    unittest.main()
