# -*- coding: utf-8 -*-
"""命令行帮助与报错的文案守卫。

为什么单独守这一层
------------------
界面、服务端报错、对外工具说明都已中文化，命令行是剩下的一处用户可见
文字。`--help` 输出与参数错误提示如果仍是英文，用户会当成"程序坏了"。

扫描守卫只看字面常量，扫不到这里的两类问题：
 1. 新增参数忘了写 help（运行时才是空的，字面量里根本没有）
 2. 命令行框架的内置消息被换回英文（它走模板拼接，不是字面量）
所以必须单独遍历解析器，并真跑一次报错。

判据：凡是**说明性文字**都必须含中文。选项名、命令动词、可选值本身是
标识符，保持英文是正确的，不在此列。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.cli import _build_parser, _cn_argparse_msg  # noqa: E402

CJK = re.compile(r"[一-龥]")


def _all_parsers():
  """返回 (名字, parser)：主解析器 + 每个子命令解析器。"""
  ap = _build_parser()
  out = [(ap.prog, ap)]
  for action in ap._actions:
    choices = getattr(action, "choices", None)
    if not isinstance(choices, dict):
      continue
    for name, sub in choices.items():
      if hasattr(sub, "add_argument"):
        out.append((f"{ap.prog} {name}", sub))
  return out


def _help_texts(parser):
  """解析器里所有说明性文字：(归属, 文案)。"""
  for action in parser._actions:
    if action.help:
      yield (action.dest, action.help)
  yield ("__description__", parser.description or "")


class HelpIsChineseTest(unittest.TestCase):
  """每个参数的说明都必须是中文。"""

  def test_every_argument_has_help(self):
    missing = []
    for name, parser in _all_parsers():
      for action in parser._actions:
        # 子命令分组本身不是参数，它的说明是分组标题（"可用命令"），
        # 由 UsageRenderingTest 单独核对
        if isinstance(action, argparse._SubParsersAction):
          continue
        if not action.help:
          missing.append(f"{name}:{action.dest}")
    self.assertEqual([], missing, f"这些参数缺少说明：{missing}")

  def test_every_help_is_chinese(self):
    bad = []
    for name, parser in _all_parsers():
      for owner, text in _help_texts(parser):
        if not text:
          continue
        if not CJK.search(text):
          bad.append(f"{name}/{owner}: {text}")
    self.assertEqual([], bad,
             "命令行说明含非中文文案：\n " + "\n ".join(bad))

  def test_subcommands_all_present(self):
    names = {n.split(" ", 1)[1] for n, _ in _all_parsers() if " " in n}
    for expect in ("distill", "run", "report", "kb", "wiki", "task",
            "skill", "mcp"):
      self.assertIn(expect, names, f"缺少子命令 {expect}")


class UsageRenderingTest(unittest.TestCase):
  """真跑 --help，确认渲染出来的也是中文。"""

  def _cli(self, *args):
    r = subprocess.run([sys.executable, "-m", "omegaforge.cli", *args],
              cwd=ROOT, capture_output=True, text=True,
              timeout=120)
    return (r.stdout + r.stderr)

  def test_usage_prefix_is_chinese(self):
    out = self._cli("--help")
    self.assertIn("用法：", out, "usage 前缀未中文化")
    self.assertNotIn("usage:", out)

  def test_section_titles_are_chinese(self):
    out = self._cli("distill", "--help")
    for title in ("位置参数：", "可选参数："):
      self.assertIn(title, out, f"缺少中文小节标题 {title}")

  def test_no_english_help_line(self):
    """说明行若整行无中文，就是没翻译到位。"""
    out = self._cli("--help")
    bad = []
    for line in out.splitlines():
      stripped = line.strip()
      if not stripped:
        continue
      # 只检查带缩进的说明行；命令名与用法行本身是标识符
      if not line.startswith("  ") and not line.startswith(" "):
        continue
      # 子命令列表那一行是标识符集合（{distill,run,...}），不是说明
      if not re.search(r"[\s\u4e00-\u9fa5]", stripped):
        continue
      if not CJK.search(stripped):
        bad.append(stripped)
    self.assertEqual([], bad, f"英文说明行：{bad}")


class ErrorMessageTranslationTest(unittest.TestCase):
  """报错模板翻译：命中要准，未命中不能把报错内容改没了。"""

  def test_known_patterns_translated(self):
    cases = [
      ("the following arguments are required: source", "缺少必填参数"),
      ("unrecognized arguments: --zzz", "无法识别的参数"),
      ("argument op: invalid choice: 'x' (choose from 'a', 'b')",
       "取值无效"),
      ("argument --priority: invalid int value: 'abc'", "整数"),
    ]
    for en, cn in cases:
      self.assertIn(cn, _cn_argparse_msg(en), f"未翻译：{en}")

  def test_values_are_preserved(self):
    """换的只是说明文字，用户填的值必须原样保留。"""
    out = _cn_argparse_msg("unrecognized arguments: --zzz")
    self.assertIn("--zzz", out)
    out = _cn_argparse_msg("the following arguments are required: source")
    self.assertIn("source", out)

  def test_unknown_pattern_is_left_alone(self):
    mystery = "some future argparse message: xyz"
    self.assertEqual(mystery, _cn_argparse_msg(mystery))

  def test_real_cli_error_is_chinese(self):
    r = subprocess.run([sys.executable, "-m", "omegaforge.cli", "kb", "zzz"],
              cwd=ROOT, capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr)
    self.assertIn("参数有误", out, f"命令行报错未中文化：{out[-200:]}")
    self.assertNotIn("invalid choice", out)


if __name__ == "__main__":
  unittest.main()
