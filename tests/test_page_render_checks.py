"""逐页渲染判定的守卫。

判定规则单独成模块，用例只需喂合成事件即可验证"规则会不会抓"。
每一条反向样本对应一种真实失效：空白页、点击未生效、渲染抛错、导航缺项。
撤掉其中任意一条断言，对应用例必须变红——否则那条断言从未验到任何东西。

期望页面清单须与 App.tsx 的导航定义一致：界面新增页面而清单不更新时，
逐页核查会少验一页而报告照写"全部通过"。

产物来源判定单独成函数（verify_bundle_source），守卫按返回的理由断言，
不按退出码——main() 在来源判定之后还会因后端未就绪、浏览器拿不到页面
等返回 1，按退出码断言的用例在来源判定被撤掉时照样通过。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from page_render_checks import EXPECTED_PAGES, evaluate, healthy_event  # noqa: E402

ALL = list(EXPECTED_PAGES)


def _events(pages):
    ev = [{"event": "nav", "labels": ALL}]
    ev.extend(pages)
    return ev


class TestPageRenderChecks(unittest.TestCase):
    def test_healthy_has_no_failure(self):
        self.assertEqual(evaluate(_events([healthy_event(p) for p in ALL])), [])

    def test_missing_nav_blocks_verdict(self):
        fails = evaluate([healthy_event(p) for p in ALL])
        self.assertTrue(any("侧边导航" in f for f in fails))

    def test_nav_missing_page(self):
        ev = [{"event": "nav", "labels": [p for p in ALL if p != "竞技场"]}]
        ev.extend(healthy_event(p) for p in ALL)
        self.assertTrue(any("缺少页面" in f for f in evaluate(ev)))

    def test_fewer_pages_than_nav(self):
        self.assertTrue(
            any("少于" in f for f in evaluate(_events([healthy_event(ALL[0])]))))

    def test_blank_page_is_caught(self):
        blank = healthy_event("竞技场")
        blank.update(nodes=2, textLen=0, sample="")
        fails = evaluate(_events([healthy_event(ALL[0]), blank]
                                 + [healthy_event(p) for p in ALL[2:]]))
        self.assertTrue(any("疑似空白" in f for f in fails))

    def test_identical_pages_mean_click_ineffective(self):
        dup = healthy_event("知识库")
        other = healthy_event("知识库")
        other["label"] = "待办"
        other["current"] = "待办"
        fails = evaluate(_events([dup, other]
                                 + [healthy_event(p) for p in ALL[2:]]))
        self.assertTrue(any("完全相同" in f for f in fails))

    def test_render_exception_is_caught(self):
        bad = healthy_event("基因组")
        bad["errors"] = ["pageerror:Cannot read properties of undefined"]
        fails = evaluate(_events([healthy_event(ALL[0]), bad]
                                 + [healthy_event(p) for p in ALL[2:]]))
        self.assertTrue(any("控制台报错" in f for f in fails))

    def test_console_error_is_caught(self):
        """错误边界接住异常后只留 console 报错，节点与文案都还达标。"""
        bad = healthy_event("基因组")
        bad["errors"] = ["console:[ErrorBoundary:应用] Error: boom"]
        fails = evaluate(_events([healthy_event(ALL[0]), bad]
                                 + [healthy_event(p) for p in ALL[2:]]))
        self.assertTrue(any("控制台报错" in f for f in fails))

    def test_current_mismatch_is_caught(self):
        wrong = healthy_event("用量")
        wrong["current"] = "设置"
        fails = evaluate(_events([healthy_event(ALL[0]), wrong]
                                 + [healthy_event(p) for p in ALL[2:]]))
        self.assertTrue(any("当前页标记" in f for f in fails))

    def test_expected_pages_match_app_nav(self):
        """界面新增页面而期望清单不更新时，逐页核查会悄悄少验一页。"""
        app = os.path.join(ROOT, "frontend", "src", "App.tsx")
        with open(app, encoding="utf-8") as f:
            src = f.read()
        labels = re.findall(r'id:\s*"[a-z]+",\s*label:\s*"([^"]+)"', src)
        self.assertTrue(labels, "未能从 App.tsx 读出导航定义")
        self.assertEqual(labels, ALL)


    def test_fingerprint_rejects_stale_bundle(self):
        """产物与源码不对应时必须拒绝，且理由必须是"源码不对应"。

        只断言退出码 1 恒真：main() 在指纹判定之后还会因后端未就绪、
        浏览器拿不到页面等返回 1，按退出码断言在判定被撤掉时照样通过。
        """
        import tempfile

        import verify_pages_browser as v

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            with open(os.path.join(src, "x.tsx"), "w") as f:
                f.write("export const A = 1;")
            dist = os.path.join(d, "dist")
            os.makedirs(dist)
            with open(os.path.join(dist, v.FINGERPRINT_NAME), "w") as f:
                f.write("stale-fingerprint\n")
            ok, reason = v.verify_bundle_source(dist, src)
            self.assertFalse(ok)
            self.assertIn("源码不一致", reason)

    def test_fingerprint_missing_is_rejected(self):
        """指纹缺失不能放行——放行等于这一层永远不生效。"""
        import tempfile

        import verify_pages_browser as v

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            with open(os.path.join(src, "x.tsx"), "w") as f:
                f.write("export const A = 1;")
            dist = os.path.join(d, "dist")
            os.makedirs(dist)
            ok, reason = v.verify_bundle_source(dist, src)
            self.assertFalse(ok)
            self.assertIn("缺少源码指纹", reason)

    def test_fingerprint_matches_after_record(self):
        """记录后必须判一致，否则每次运行都会误报漂移、整层被绕过。"""
        import tempfile

        import verify_pages_browser as v

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(os.path.join(src, "a"))
            with open(os.path.join(src, "a", "x.tsx"), "w") as f:
                f.write("export const A = 1;")
            dist = os.path.join(d, "dist")
            os.makedirs(dist)
            with open(os.path.join(dist, v.FINGERPRINT_NAME), "w") as f:
                f.write(v._tree_fingerprint(src) + "\n")
            self.assertEqual(v.verify_bundle_source(dist, src), (True, ""))

    def test_fingerprint_changes_when_source_changes(self):
        """指纹若对改动不敏感，整层会退化成恒真。"""
        import tempfile

        import verify_pages_browser as v

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            target = os.path.join(src, "x.tsx")
            with open(target, "w") as f:
                f.write("export const A = 1;")
            before = v._tree_fingerprint(src)
            with open(target, "w") as f:
                f.write("export const A = 2;")
            self.assertNotEqual(before, v._tree_fingerprint(src))


if __name__ == "__main__":
    unittest.main()
