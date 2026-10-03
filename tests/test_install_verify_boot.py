# -*- coding: utf-8 -*-
"""安装后启动判定：存活状态必须在回收进程之前取。

为什么单独守这一条
------------------
判据写反的后果不是"漏报"，而是**把每一次成功启动都报成失败**：

    探测到端口可连 → kill 进程 → 再判 poll() is None
    → poll() 必然非 None → 报「端口可连但进程已退出，连上的不是本 sidecar」

而这条报错的文本指向"安装布局或依赖有问题"，排查方向会被带到打包配置上，
真正的错在验证脚本自己把进程杀了。

判定顺序无法靠读代码看出来（两处都在同一函数里、相距十余行），
故用源码断言钉住：取存活状态的赋值必须出现在 kill 之前。
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY = os.path.join(ROOT, "scripts", "ci_verify_install.py")


def _src() -> str:
    with open(VERIFY, encoding="utf-8") as f:
        return f.read()


class BootVerdictOrderTest(unittest.TestCase):
    def test_source_readable(self):
        self.assertTrue(_src().strip(), "读不到验证脚本 —— 读不到不能判通过")

    def test_alive_sampled_before_kill(self):
        src = _src()
        alive = src.find("alive_at_ready =")
        kill = src.find("proc.kill()")
        self.assertGreater(alive, 0, "未取就绪时的存活状态")
        self.assertGreater(kill, 0, "未回收进程")
        self.assertLess(
            alive, kill,
            "存活状态在 kill 之后才取 —— 每次成功启动都会被报成失败")

    def test_verdict_uses_sampled_alive(self):
        """判定必须用采样值，不能用 kill 之后的 poll()。"""
        src = _src()
        self.assertIn("if ready and not alive_at_ready:", src)
        # 判定处不得再出现 proc.poll()
        for m in re.finditer(r"^.*alive_at_ready.*$", src, re.M):
            if "if ready" in m.group(0):
                self.assertNotIn("proc.poll()", m.group(0))

    def test_error_text_points_at_real_cause(self):
        """报错文本不得把顺序错误说成布局问题——
        那样会让人去查打包配置，而错在验证脚本自身。"""
        src = _src()
        i = src.find("端口可连但进程已退出")
        self.assertGreater(i, 0)
        seg = src[i:i + 200]
        self.assertNotIn("布局", seg)
        self.assertNotIn("依赖", seg)


if __name__ == "__main__":
    unittest.main()
