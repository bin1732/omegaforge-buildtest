# -*- coding: utf-8 -*-
"""用户可见指引必须指向真实存在的界面入口，且那里真有对应的动作。

为什么单独守这一条
------------------
面向用户的报错以「请在设置页的语音面板按提示安装」收尾时，功能验收只看
接口通不通：接口是通的，验收全绿。而用户被引到设置页后若找不到任何安装
入口，这条指引就是空转——他重试一百次也不会成功。

故这里既查"指引指向的位置存在"，也查"那个位置里真有对应动作"。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import guidance_targets as gt  # noqa: E402


VOICE_PANEL = "frontend/src/components/VoicePanel.tsx"
SETTINGS = gt.PAGE_OF["设置页"]


class VoiceGuidanceTest(unittest.TestCase):
    """语音未就绪时给用户的指引，必须指向真实存在的面板。"""

    def _texts(self) -> list[str]:
        from omegaforge.voice import asr, tts
        out = []
        for mod in (asr, tts):
            src = open(mod.__file__, encoding="utf-8").read()
            for m in __import__("re").finditer(r'UserError\(\s*"([^"]+)"', src):
                out.append(m.group(1))
        return out

    def test_voice_texts_are_collected(self):
        """取不到文案时不能判通过——那会让后面所有断言恒真。"""
        self.assertTrue(self._texts(), "未能取到语音模块的用户可见文案")

    def test_guidance_target_exists(self):
        for t in self._texts():
            for bad in gt.unreachable(t):
                self.fail(bad)

    def test_panel_provides_install_command(self):
        self.assertTrue(
            gt.panel_provides_action(VOICE_PANEL, "install"),
            "语音面板未给出可执行的安装命令——指引让用户去装，却没说装什么")

    def test_panel_provides_model_source(self):
        self.assertTrue(
            gt.panel_provides_action(VOICE_PANEL, "source"),
            "语音面板未给出模型获取来源——指引指向不存在的仓库文档等于空转")

    def test_settings_page_renders_voice_panel(self):
        src = gt._read(os.path.join(ROOT, SETTINGS))
        self.assertTrue(src, f"读不到 {SETTINGS}")
        self.assertIn("VoicePanel", src)


class ExtractorTest(unittest.TestCase):
    """取目标这一步的判定要能被反例证伪。

    恒真的判定会让失效永久通过，故每个分支都配一个反例。
    """

    def test_extracts_place_and_panel(self):
        self.assertIn(("设置页", "语音"),
                      gt.extract_targets("请在设置页的语音面板按提示安装后重试"))

    def test_no_target_when_absent(self):
        self.assertEqual([], gt.extract_targets("输入不合法，请修改后重试"))

    def test_unregistered_alias_is_reported_as_undeterminable(self):
        """中文面板名没登记英文别名时必须报"无法判定"，
        而不是静默判成可达——后者会让整条守卫恒真。"""
        bad = gt.unreachable("请在设置页的量子面板里操作")
        self.assertTrue(bad)
        self.assertTrue(any("未登记" in b for b in bad), bad)

    def test_registered_but_missing_panel_is_reported(self):
        """登记了别名、但页面没引用该组件：指引指向的入口不存在。"""
        bad = gt.unreachable("请在设置页的供应商面板里操作")
        self.assertTrue(bad)
        self.assertTrue(any("未引用匹配" in b for b in bad), bad)

    def test_voice_guidance_resolves_to_real_panel(self):
        """语音文案必须真能解析出目标，且判定可达——
        只断言"没有坏消息"的话，解析失效时会静默通过。"""
        targets = [t for t in VoiceGuidanceTest("test_voice_texts_are_collected")._texts()
                   if gt.extract_targets(t)]
        self.assertTrue(targets, "语音文案里未解析出任何指引目标")
        for t in targets:
            self.assertEqual([], gt.unreachable(t))

    def test_missing_page_is_not_treated_as_reachable(self):
        orig = gt.PAGE_OF
        gt.PAGE_OF = {"设置页": "frontend/src/pages/Nope.tsx"}
        try:
            bad = gt.unreachable("请在设置页的语音面板里操作")
            self.assertTrue(bad)
            self.assertTrue(any("读不到" in b for b in bad))
        finally:
            gt.PAGE_OF = orig


if __name__ == "__main__":
    unittest.main()
