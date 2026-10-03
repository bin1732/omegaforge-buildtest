"""工具入口在畸形入参下的文案守卫。

## 为什么需要这一层

工具入口原本对两类畸形入参都抛非 UserError 的异常，被统一转译层压成
"请求内容有误，请检查后重试"与"操作失败，请稍后重试"。使用者照着这两句
只会重复提交，而工具名写错、参数格式不对，重复多少次都不会自己变对。

两条约束各自独立：

1. 工具名不存在要点名填了什么，并给出可选用什么——只说"内容有误"，
   使用者无法定位是名称错还是参数错。
2. 参数不是字典必须单独报格式问题——它发生在取字段之前，若并入名称
   校验，正常工具配上畸形参数会报出错误的原因。

## 回退校验点（撤掉修复，下列用例必须变红）

  · 抛 ValueError 而非 UserError        -> 1/2/3 变红
  · 参数校验放在取字段之后              -> 2 变红
  · 工具名校验只说"请求内容有误"        -> 1 变红
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _Home:
    """隔离数据目录，避免守卫改动到使用者的真实数据。"""

    def setUp(self) -> None:
        self._snap = os.environ.get("OMEGAFORGE_HOME")
        os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_tool_")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._snap is None:
            os.environ.pop("OMEGAFORGE_HOME", None)
        else:
            os.environ["OMEGAFORGE_HOME"] = self._snap


class ToolDispatchTextTest(_Home, unittest.TestCase):
    def _text(self, name: str, args) -> str:
        from omegaforge.core.errors import user_error
        from omegaforge.tools.system_tools import tool_dispatch

        try:
            tool_dispatch(name, args)
        except BaseException as exc:  # noqa: BLE001 - 任意异常都要转译后判定
            return user_error(exc)
        return ""

    def test_unknown_tool_names_what_and_options(self) -> None:
        """工具名不存在：要点名填了什么，并给出可用工具。"""
        text = self._text("不存在的工具", {})
        self.assertIn("不存在的工具", text, f"要点名填错的值：{text!r}")
        self.assertIn("可用工具", text, f"要给出可选项：{text!r}")
        for vague in ("请求内容有误", "操作失败", "请稍后重试"):
            self.assertNotIn(vague, text, f"不得落到无信息量兜底：{text!r}")

    def test_non_dict_args_reports_format(self) -> None:
        """参数不是字典：必须单独报格式问题，而不是崩在取字段时。"""
        text = self._text("run_command", "notadict")
        self.assertIn("格式", text, f"要说清是格式问题：{text!r}")
        self.assertNotIn("操作失败", text, f"不得落到内部故障文案：{text!r}")

    def test_text_is_chinese_without_internal_tokens(self) -> None:
        """两类文案都必须是中文，且不带英文技术串。"""
        unknown = self._text("x", {})
        self.assertIn("x", unknown,
                      f"未知工具名时要点名填了什么：{unknown!r}")
        for text in (unknown, self._text("run_command", 5)):
            self.assertTrue(any("一" <= ch <= "龥" for ch in text),
                            f"必须是中文：{text!r}")
            for bad in ("unknown", "Error", "Traceback", "NoneType"):
                self.assertNotIn(bad, text, f"不得回显内部串：{text!r}")


if __name__ == "__main__":
    unittest.main()
