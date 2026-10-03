"""交付脚本目标仓库的守卫。

推送脚本把私人仓写死成默认目标时，一次误调用就会在验证完成之前覆盖
交付物——覆盖是强制的，没有回退，且不报错。

两条判据各配一个反例：撤掉哪条，对应用例必须变红。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import check_delivery_target as c  # noqa: E402

PRIVATE = "bin1732/omegaforge"


def _write(tmp: str, body: str, name: str = "scripts/push_x.py") -> str:
    full = os.path.join(tmp, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(body)
    return full


class DeliveryTargetTest(unittest.TestCase):
    def test_script_passes_on_current_tree(self):
        """真实脚本须合规。"""
        for rel in c.SCRIPTS:
            full = os.path.join(ROOT, rel)
            self.assertTrue(os.path.isfile(full), f"{rel} 缺失")
            self.assertEqual(c.check(full), [], f"{rel} 目标不受控")

    def test_private_default_is_reported(self):
        """默认值写死私人仓必须被报出。"""
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, f'REPO = "{PRIVATE}"\n')
            self.assertTrue(any("默认值" in x for x in c.check(p)))

    def test_private_default_via_env_get_is_reported(self):
        """经环境变量取默认值同样不受控。"""
        with tempfile.TemporaryDirectory() as d:
            body = f'REPO = os.environ.get("PUSH_REPO", "{PRIVATE}")\n'
            self.assertTrue(any("默认值" in x for x in c.check(_write(d, body))))

    def test_guard_constant_is_not_treated_as_default(self):
        """PRIVATE_REPO 常量是判据本身，误报会让整段守卫被绕过。"""
        with tempfile.TemporaryDirectory() as d:
            body = (f'PRIVATE_REPO = "{PRIVATE}"\n'
                    'REPO = os.environ.get("PUSH_REPO", "")\n'
                    'if REPO == PRIVATE_REPO and '
                    'os.environ.get("ALLOW_PRIVATE_PUSH") != "1":\n'
                    '    sys.exit(1)\n')
            self.assertEqual(c.check(_write(d, body)), [])

    def test_missing_release_gate_is_reported(self):
        """出现私人仓却无显式放行判据，必须报出。"""
        with tempfile.TemporaryDirectory() as d:
            body = (f'PRIVATE_REPO = "{PRIVATE}"\n'
                    'REPO = os.environ.get("PUSH_REPO", "")\n'
                    f'if REPO == PRIVATE_REPO:\n    print("{PRIVATE}")\n')
            self.assertTrue(any("显式放行判据" in x for x in c.check(_write(d, body))))

    def test_missing_script_is_reported(self):
        """脚本缺失不能判通过——那等于这一层从不生效。"""
        orig_scripts, orig_root = c.SCRIPTS, c.ROOT
        try:
            c.SCRIPTS = ["scripts/push_nope.py"]
            self.assertEqual(c.main(), 1)
        finally:
            c.SCRIPTS, c.ROOT = orig_scripts, orig_root


if __name__ == "__main__":
    unittest.main()
