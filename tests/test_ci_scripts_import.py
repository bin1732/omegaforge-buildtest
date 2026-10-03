"""CI 脚本必须能被导入。

卸载验收曾因为引用了不存在的名字，只在 Windows 装机实例上以 ImportError
退出——而那台机器是它唯一会执行的地方。脚本本体无法在沙盒里实跑，但
"导入即失败"这一类缺陷可以在沙盒里拦住：模块级代码都执行不到，就不可能
跑到真正的判定。

本用例不验脚本的判定是否正确（那由各自的用例负责），只守住一条底线：
被引用的名字必须真的存在。

只验导入不够：函数体里的名字要等那一步真的跑到才暴露，而 CI 脚本往往
只在 Windows 装机实例上执行。所以这里再静态核一遍脚本引用到的共用模块
成员——导入成功但成员名写错，同样会在那台机器上以 AttributeError 退出。
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

PREFIXES = ("ci_verify_", "verify_", "check_")


def refs_from_src(src: str) -> set[str]:
    """从脚本源码里提取对共用模块的属性访问（ilc.X / install_layout_checks.X）。

    只认 AST 里的真实属性访问。正则扫全文会把注释中的文件名
    install_layout_checks.py 读成"引用了成员 py"，报出一个并不存在的
    缺陷——假报会把人派去改一段本来正确的实现，比不报更坏。
    """
    tree = ast.parse(src)          # 解析不了就抛，绝不静默放行
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id in (
                "ilc", "install_layout_checks"):
            out.add(node.attr)
    return out


def _targets() -> list[str]:
    out = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py"):
            continue
        if not any(name.startswith(p) for p in PREFIXES):
            continue
        if name.startswith("__"):
            continue
        out.append(name[:-3])
    return out


class TestCiScriptsImport(unittest.TestCase):
    def test_targets_not_empty(self):
        """清单为空时此用例恒真——读不到脚本不能当作全部合格。"""
        self.assertGreaterEqual(len(_targets()), 8)

    def test_every_script_imports(self):
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        failed = []
        for mod in _targets():
            try:
                importlib.import_module(mod)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{mod}: {type(exc).__name__}: {exc}")
        self.assertEqual(failed, [],
                         "以下脚本导入失败（在它唯一会执行的机器上会直接退出）：\n  "
                         + "\n  ".join(failed))


    def test_shared_module_members_exist(self):
        """脚本引用到的共用模块成员必须真的存在。

        函数体里的名字不会在导入时暴露，只会在那台唯一的执行机上以
        AttributeError 退出。成员清单为空时同样判失败——读不到不能当作
        全都对得上。
        """
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        import install_layout_checks as ilc  # noqa: E402

        # 只认**代码里真实的属性访问**：早期用正则扫全文，会把注释里提到的
        # 文件名 install_layout_checks.py 读成"引用了成员 py"，于是报出一个
        # 并不存在的缺陷——而被点的那处偏偏在讲"失效发生在辅助模块上"。
        # 假报比不报更坏：它把人派去改一段本来正确的实现。
        refs: dict[str, set[str]] = {}
        for mod in _targets():
            src = open(os.path.join(SCRIPTS, mod + ".py"), encoding="utf-8").read()
            found = refs_from_src(src)
            if found:
                refs[mod] = found
        self.assertTrue(refs, "未提取到任何成员引用——正则失配会让此用例恒真")

        bad = [f"{m}.{n}" for m, ns in sorted(refs.items())
               for n in sorted(ns) if not hasattr(ilc, n)]
        self.assertEqual(bad, [], "以下成员在共用模块里不存在：\n  " + "\n  ".join(bad))


class TestRefExtraction(unittest.TestCase):
    """提取口径自身的反验：真缺陷要报，注释里的文件名不得报。"""

    def test_real_attribute_access_is_captured(self):
        src = "import install_layout_checks as ilc\n" \
              "def f():\n    return ilc.layout_problems(x)\n"
        self.assertIn("layout_problems", refs_from_src(src))

    def test_filename_in_comment_is_not_a_member(self):
        """注释里的 install_layout_checks.py 曾被读成"引用了成员 py"。

        这条失效曾让架构步骤在 CI 上连红三轮，而它报的那处实现本来是对的。
        """
        src = ('def f():\n    """失效发生在辅助模块上'
               '（install_layout_checks.py 既不匹配 ci_*）。"""\n'
               '    return 1\n')
        self.assertEqual(refs_from_src(src), set(),
                         "注释/文档字符串里的文件名不得被当成成员引用")

    def test_unparsable_source_raises(self):
        """解析不了必须抛：静默返回空集会退化成"什么都合格"。"""
        with self.assertRaises(SyntaxError):
            refs_from_src("def f(:\n")


if __name__ == "__main__":
    unittest.main()
