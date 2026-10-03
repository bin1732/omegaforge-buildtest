# -*- coding: utf-8 -*-
"""后端能力必须有可达出口——能力存在但用户够不着，等于假功能。

每个分支都配一个反例：恒真的判定会让失效永久通过，
故"未登记""陈旧登记""理由为空"三种放行都必须能被证伪。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_endpoint_surface as ces  # noqa: E402


class SurfaceTest(unittest.TestCase):
    def test_no_problem_in_repo(self):
        self.assertEqual([], ces.problems(), "\n".join(ces.problems()))

    def test_routes_are_readable(self):
        """取不到路由时不能判通过——那会让所有断言恒真。"""
        self.assertTrue(ces.backend_routes())
        self.assertTrue(ces.frontend_source())

    def test_compare_has_real_entry_not_registration(self):
        """compare 是靠真实前端入口通过的，不是靠登记绕过去的。"""
        self.assertNotIn("/api/compare", ces.CLI_ONLY)
        self.assertTrue(ces.has_frontend_entry("/api/compare", ces.frontend_source()))

    def test_registered_entries_have_reason(self):
        for route, why in ces.CLI_ONLY.items():
            self.assertTrue(why.strip(), f"{route} 的登记理由为空")

    def test_registered_routes_exist_in_backend(self):
        """登记表里不能有拼写错误或已删除的端点——
        那样登记会永久埋在表里，谁也不会再看。"""
        for route in ces.CLI_ONLY:
            self.assertIn(route, ces.backend_routes(), f"{route} 不是后端路由")


class DetectorTest(unittest.TestCase):
    """判定本身要能被反例证伪。"""

    def test_unregistered_route_is_reported(self):
        orig = ces.backend_routes
        ces.backend_routes = lambda: ["/api/definitely/not/here"]
        try:
            bad = ces.problems()
            self.assertTrue(any("未登记" in b for b in bad), bad)
        finally:
            ces.backend_routes = orig

    def test_stale_registration_is_reported(self):
        orig = ces.CLI_ONLY
        ces.CLI_ONLY = {"/api/compare": "曾经不经界面"}
        try:
            bad = ces.problems()
            self.assertTrue(any("陈旧" in b for b in bad), bad)
        finally:
            ces.CLI_ONLY = orig

    def test_empty_reason_is_reported(self):
        orig = ces.CLI_ONLY
        ces.CLI_ONLY = {"/api/tools/exec": "   "}
        try:
            bad = ces.problems()
            self.assertTrue(any("理由为空" in b for b in bad), bad)
        finally:
            ces.CLI_ONLY = orig

    def test_template_literal_counts_as_entry(self):
        """`/api/jobs/${id}` 这类拼接必须算有入口，
        只比整条字面量会把它们全判成没有入口。"""
        self.assertTrue(
            ces.has_frontend_entry("/api/report/x", "get(`/api/report/${id}`)"))

    def test_unreadable_frontend_is_not_treated_as_clean(self):
        orig = ces.frontend_source
        ces.frontend_source = lambda: ""
        try:
            bad = ces.problems()
            self.assertTrue(any("读不到前端" in b for b in bad), bad)
        finally:
            ces.frontend_source = orig


if __name__ == "__main__":
    unittest.main()
