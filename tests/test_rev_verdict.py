"""判定助手的守卫：用例标识的解析必须扛得住参数里的分隔符。

pytest 的用例标识用 `::` 分段（文件路径::类名::用例名[参数]），而参数本身
可能含 `::`——内网地址用例的参数就是 IPv6 字面量（`http://[::1]/`）。按
`::` 取最后一段会得到 `1]/]`，与任何期望都对不上，于是正常的失败被判成
"预期之外还失败"，校验点从"抓到"翻成"未抓到"。

这个翻面方向很危险：它表现为校验失败，排查时容易被当成"这处修复没生效"
而去改被测代码，实际坏的是判定本身。所以解析规则要有常驻守卫。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes._rev_verdict import verdict  # noqa: E402


@pytest.mark.parametrize("ident, expect", [
    # 参数里含 "::"（IPv6 字面量）：必须仍能与用例名对上
    ("tests/t.py::test_internal_url_blocked[http://[::1]/]",
     "tests/t.py::test_internal_url_blocked"),
    # 参数化后缀：同一用例的不同参数实例按基名归并
    ("tests/t.py::test_blocked[rm -rf /]",
     "tests/t.py::test_blocked"),
    # 带类名的标识
    ("tests/t.py::TestX::test_y", "tests/t.py::test_y"),
    # 参数里含 "::" 且带类名
    ("tests/t.py::TestX::test_u[http://[::1]/]", "tests/t.py::test_u"),
])
def test_leaf_matches_expectation(ident, expect):
    ok, why = verdict(1, [ident], [expect])
    assert ok, why


@pytest.mark.parametrize("ident, expect, should", [
    # 期望写实例后缀：只认该实例，另一个实例不得被当成同一条
    ("tests/t.py::test_m[skill]", "tests/t.py::test_m[skill]", True),
    ("tests/t.py::test_m[wrap]", "tests/t.py::test_m[skill]", False),
    # 期望不写后缀：仍按基名归并，任一实例都算命中
    ("tests/t.py::test_m[wrap]", "tests/t.py::test_m", True),
])
def test_instance_qualified_expectation(ident, expect, should):
    """点名到参数实例时只认该实例。

    同一条用例的不同参数实例可能对应完全不同的被测分支。只按基名归并的话，
    撤掉其中一种边界标记的防伪造，会被另一种实例的成功掩盖成"抓到"。
    """
    ok, why = verdict(1, [ident], [expect])
    assert ok is should, why


def test_subtest_failure_counts_as_its_parent(tmp_path):
    """子测试的失败必须计入其父用例。

    子测试失败走 "SUBFAILED[param] path::test" 而不是 FAILED 行。不计入的
    话，一个全部以子测试形式失败的用例会被当成"没失败"，校验点从抓到翻成
    未抓到——而翻面方向表现为校验失败，容易被当成"这处修复没生效"去改
    被测代码，实际坏的是解析。这里直接跑一次真 pytest 来验，不模拟输出。
    """
    d = tmp_path
    (d / "test_sub.py").write_text(
        "import unittest\n"
        "\n"
        "\n"
        "class T(unittest.TestCase):\n"
        "    def test_x(self):\n"
        "        for p in (1, 2):\n"
        "            with self.subTest(p=p):\n"
        "                self.fail('boom')\n",
        encoding="utf-8")
    from probes._rev_verdict import pytest_run
    rc, failed, tail = pytest_run(str(d / "test_sub.py"))
    assert rc != 0, tail
    ok, why = verdict(rc, failed, ["test_sub.py::test_x"])
    assert ok, why


def test_missing_expected_is_not_caught():
    """失败的是别的用例时，不能判抓到。"""
    ok, _ = verdict(1, ["tests/t.py::test_other"], ["tests/t.py::test_x"])
    assert not ok


def test_no_failed_lines_is_not_caught():
    """rc 非零却没有任何失败行，多为收集阶段就失败，不可作为证据。"""
    ok, why = verdict(2, [], ["tests/t.py::test_x"])
    assert not ok
    assert "没有失败用例" in why


def test_unexpected_failures_are_rejected():
    """整片都红时判未抓到——那种红是环境故障，不是守卫在起作用。"""
    ok, why = verdict(1, ["tests/t.py::test_x[a]", "tests/t.py::test_y"],
                      ["tests/t.py::test_x"])
    assert not ok
    assert "预期之外" in why


def test_restore_invalidates_stale_bytecode(tmp_path):
    """还原备份后必须读到还原后的源码，不能仍是注入版的字节码。

    备份用 copy2、还原用 move，**两者都保留原文件的 mtime**；而 Python
    判断是否复用 `.pyc` 只看源码 mtime + 大小。当注入与还原后的源码长度
    恰好相同时（例如 `return 1` 与 `return 0`），还原之后缓存里记录的
    mtime 与大小仍然吻合——后续执行的是**注入期间编译出的字节码**。

    这时源码看着已还原、基线也报全绿，而绿的是注入版的行为。所以还原
    必须额外刷新 mtime 并删掉对应缓存文件。
    """
    import shutil
    import subprocess
    import textwrap
    from pathlib import Path

    from probes._rev_verdict import restore_src

    mod = tmp_path / "probe_mod.py"
    good = 'VALUE = 1\n'
    bad = 'VALUE = 2\n'      # 与 good 等长：只有长度相同才会踩到这个坑
    assert len(good) == len(bad)
    mod.write_text(good, encoding="utf-8")

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    target = pkg / "probe_mod.py"
    shutil.copy2(mod, target)           # 备份：保留 mtime
    bak = tmp_path / "probe_mod.py.bak"
    shutil.copy2(mod, bak)

    # 注入 + 导入：这一步会在 __pycache__ 里留下注入版的字节码
    target.write_text(bad, encoding="utf-8")
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from pkg.probe_mod import VALUE\nprint(VALUE)\n" % str(tmp_path))
    out1 = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True)
    assert out1.stdout.strip() == "2", "注入未能生效，本用例前提不成立"

    restore_src(target, bak)            # 还原：等长 + 同 mtime 的旧坑
    out2 = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True)
    assert out2.stdout.strip() == "1", (
        "还原后仍读到注入版的行为：字节码缓存没失效，"
        "「源码已还原」是假的")


def test_plain_move_leaves_stale_bytecode(tmp_path):
    """上一条的反向：只做 move（保留 mtime）确实会留下旧字节码。

    这条不是为了证明 move 有问题，而是证明**上一条守卫验的是真隐患**——
    若只做 move 也能读到还原后的值，上一条就成了恒真断言。
    """
    import shutil
    import subprocess
    from pathlib import Path

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    target = pkg / "probe_mod2.py"
    src = tmp_path / "probe_mod2.py"
    src.write_text("VALUE = 1\n", encoding="utf-8")
    shutil.copy2(src, target)
    bak = tmp_path / "probe_mod2.py.bak"
    shutil.copy2(src, bak)

    target.write_text("VALUE = 2\n", encoding="utf-8")
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from pkg.probe_mod2 import VALUE\nprint(VALUE)\n" % str(tmp_path))
    assert subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True).stdout.strip() == "2"

    shutil.move(str(bak), str(target))  # 只 move：mtime 与大小都没变
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True)
    assert out.stdout.strip() == "2", (
        "只做 move 却读到了还原后的值：说明同尺寸还原不会留下旧字节码，"
        "上一条守卫的前提已不成立，须重定位")

def test_leaf_strips_empty_path_segment():
    """标识以 "::" 开头时仍要归一到用例名。

    跨盘符时相对路径算不出来，pytest 打出的标识会缺掉文件那一段：
    "::T::test_x" 而不是 "test_sub.py::T::test_x"。不处理空段的话，同一条
    用例在 Linux 上归一成 test_x、在 Windows 上归一成 T::test_x，点名对不上，
    校验点从"抓到"翻成"未抓到"——翻面方向会被当成修复没生效，实际坏的是解析。
    """
    ok, why = verdict(1, ["::T::test_x"], ["test_sub.py::test_x"])
    assert ok, why
    ok, why = verdict(1, ["::T::test_x"], ["::T::test_x"])
    assert ok, why
