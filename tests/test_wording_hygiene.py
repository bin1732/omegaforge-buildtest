# -*- coding: utf-8 -*-
"""面向用户文案的整洁度守卫。

守三条：
 1. 用户可见文案里不得出现内部标识与开发用语（由扫描探测脚本判定）
 2. 每个对外工具的参数都必须登记中文名，不得回显英文标识
 3. 对外工具的说明必须是中文，且不得夹带协议字段名当解释

第 2、3 条为什么必须单独守：扫描探测脚本只看"已经写出来的字面量"，
而新增参数若忘了登记中文名，报错时才会在用户眼前冒出英文——
那时探测脚本扫不到（它不在字面量里，是运行时拼出来的）。
所以"登记覆盖率"要单独钉住。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.mcp_server import McpCore, _cn_param     # noqa: E402
from omegaforge.mcp_server import _CN_PARAM, _CN_PARAM_ANY   # noqa: E402


def _tools():
  return McpCore.TOOLS


class ParamCnNameCoverageTest(unittest.TestCase):
  """每个参数都要有中文名——否则报错时会把英文标识甩给调用方。"""

  def test_every_param_has_cn_name(self):
    for spec in _tools():
      tool = spec["name"]
      schema = spec.get("inputSchema") or {}
      props = schema.get("properties") or {}
      for key in props:
        cn = _cn_param(tool, key)
        self.assertTrue(
          re.search(r"[\u4e00-\u9fff]", cn),
          f"工具 {tool} 的参数 {key} 没有登记中文名（当前回显「{cn}」）")

  def test_cn_names_not_empty(self):
    """登记了但取不到（表结构写错）也要能发现。"""
    for tool, mapping in _CN_PARAM.items():
      self.assertIsInstance(mapping, dict, f"{tool} 的中文名表结构错误")
      for key, cn in mapping.items():
        self.assertTrue(cn.strip(), f"{tool}.{key} 的中文名为空")

  def test_no_stale_tool_entry(self):
    """表里登记的工具必须真实存在——否则改名后旧的登记变成死条目。"""
    names = {t["name"] for t in _tools()}
    for tool in _CN_PARAM:
      self.assertIn(tool, names, f"中文名表里有不存在的工具 {tool}")


class ToolDescriptionTest(unittest.TestCase):
  """对外说明必须是中文，且不能把字段名当解释。"""

  def test_description_is_chinese(self):
    for spec in _tools():
      desc = spec.get("description") or ""
      self.assertTrue(
        re.search(r"[\u4e00-\u9fff]", desc),
        f"工具 {spec['name']} 的说明不是中文：{desc[:60]}")

  def test_description_has_no_bare_args_clause(self):
    """不得出现 `Args: x, y` 这种纯英文参数罗列。"""
    for spec in _tools():
      desc = spec.get("description") or ""
      self.assertNotIn("Args:", desc,
               f"工具 {spec['name']} 的说明含英文参数罗列")


class UserFacingWordingScanTest(unittest.TestCase):
  """全量扫描：用户可见文案里不得有内部标识与开发用语。"""

  def test_scan_is_clean(self):
    sys.path.insert(0, os.path.join(ROOT, "probes"))
    import probe_user_facing_wording as probe   # noqa: E402

    offenders = []
    for _name, items in (("后端", probe._py_user_error_texts()),
               ("前端", probe._frontend_zh_texts())):
      for path, text in items:
        bad = probe._offenders(text)
        if bad:
          offenders.append((path, text, bad))
    self.assertEqual(
      [], offenders,
      "用户可见文案含内部标识或开发用语：\n" + "\n".join(
        f" {os.path.relpath(p, ROOT)}: {t[:70]} -> {b}"
        for p, t, b in offenders[:20]))
