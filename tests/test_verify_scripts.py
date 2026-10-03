"""验收脚本起跑检查自身的守卫。

这些用例验的是检查本身，不是产品。若检查退化为恒真，它会每次都通过却什么
都没查——历史上这类退化只表现为"一直绿"，没有任何别的症状。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_verify_scripts as cvs  # noqa: E402


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="vscheck_"))


class TestRealScripts(unittest.TestCase):
    """真实脚本目录必须通过。"""

    def test_real_scripts_all_importable(self):
        problems = cvs.check(ROOT / "scripts")
        self.assertEqual(problems, [], f"真实脚本目录应无问题，实际：{problems}")


class TestGuardIsNotVacuous(unittest.TestCase):
    """检查不能因为查了零个脚本而通过。"""

    def test_empty_dir_is_reported(self):
        d = _tmpdir()
        problems = cvs.check(d)
        self.assertTrue(problems, "空目录必须判失败——空集通过等于检查恒真")
        joined = "\n".join(problems)
        self.assertIn("下限", joined)

    def test_too_few_scripts_is_reported(self):
        d = _tmpdir()
        (d / "ci_verify_one.py").write_text("x = 1\n", encoding="utf-8")
        problems = cvs.check(d)
        self.assertTrue(problems, "脚本数低于下限必须判失败")


class TestCatchesMissingMember(unittest.TestCase):
    """引用不存在的成员必须被点名。这是历史上真实发生的失效形态。"""

    def test_missing_member_is_named(self):
        d = _tmpdir()
        (d / "install_layout_checks.py").write_text(
            "def find_file(root, prefix, suffix):\n    return ''\n",
            encoding="utf-8",
        )
        (d / "ci_verify_uninstall.py").write_text(
            "from install_layout_checks import find_sidecar\n",
            encoding="utf-8",
        )
        problems = cvs.missing_members(d)
        self.assertEqual(len(problems), 1, f"应恰好报一条：{problems}")
        self.assertIn("find_sidecar", problems[0])
        self.assertIn("install_layout_checks", problems[0])

    def test_existing_member_is_not_reported(self):
        d = _tmpdir()
        (d / "helper_mod.py").write_text(
            "CONF = 1\n"
            "def find_file(a, b):\n    return ''\n",
            encoding="utf-8",
        )
        (d / "ci_verify_install.py").write_text(
            "from helper_mod import CONF, find_file\n",
            encoding="utf-8",
        )
        self.assertEqual(cvs.missing_members(d), [])

    def test_third_party_import_is_not_checked_here(self):
        """第三方模块不由这层负责，交由导入检查。"""
        d = _tmpdir()
        (d / "ci_verify_x.py").write_text("import os\n", encoding="utf-8")
        self.assertEqual(cvs.missing_members(d), [])


class TestCatchesImportFailure(unittest.TestCase):
    """导入期崩溃必须被点名，并带上原因。"""

    def test_import_error_is_reported_with_reason(self):
        d = _tmpdir()
        (d / "ci_verify_boom.py").write_text(
            "raise RuntimeError('kaboom')\n", encoding="utf-8"
        )
        problems = cvs.import_failures(d)
        self.assertEqual(len(problems), 1, f"应恰好报一条：{problems}")
        self.assertIn("ci_verify_boom.py", problems[0])
        self.assertIn("kaboom", problems[0], "只报失败不报原因的诊断等于没有诊断")

    def test_timeout_is_reported(self):
        """安静地等很久也必须判失败，而不是被当成通过。"""
        d = _tmpdir()
        (d / "ci_verify_hang.py").write_text(
            "import time\ntime.sleep(60)\n", encoding="utf-8"
        )
        saved = cvs.IMPORT_TIMEOUT
        cvs.IMPORT_TIMEOUT = 2
        try:
            problems = cvs.import_failures(d)
        finally:
            cvs.IMPORT_TIMEOUT = saved
        self.assertEqual(len(problems), 1)
        self.assertIn("超时", problems[0])


if __name__ == "__main__":
    unittest.main()
