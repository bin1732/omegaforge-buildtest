"""夹具导出值的 shell 回读守卫。

为什么必须有这一层：
    夹具文件用 `. 文件` 载入。bash 把反斜杠当转义符，未加引号的
    `export V=D:\\a\\b` 载入后变成 `D:ab`。后端拿这个被吃掉的路径去查
    技能说明文件，报「缺少技能说明文件」——症状像"安装功能坏了"，
    排查方向完全相反（run90 真实失败日志）。

    Python 侧自检查不出这类错：它看到的是未经 shell 处理的原值，
    isfile 判定为真，而消费方拿到的是另一个字符串。

必须逐条反验：
    - 未加引号的写法必须被抓到（否则本守卫恒真）
    - 加引号的写法必须回读一致
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

sys_path = Path(__file__).resolve().parents[1] / "scripts"
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "make_journey_fixtures", sys_path / "make_journey_fixtures.py"
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


pytestmark = pytest.mark.skipif(
    not m.resolve_bash(), reason="需要真正可用的 bash 才能验证 shell 载入语义"
)

# 构造 runner 上真实形态的 Windows 路径：含反斜杠，且必然触发转义。
WIN_A = r"D:\a\omegaforge-buildtest\omegaforge-buildtest\ci_diag\journey\jskill-a"


def _roundtrip(line: str, var: str) -> str:
    """把一行 export 交给真实 bash 载入，取回变量的值。

    不能用字面量 "bash"：Windows 的 CreateProcess 会先命中
    C:\\Windows\\System32\\bash.exe（WSL 启动器），未装发行版时它把提示
    打到 stdout 而不报错，于是比对的一方永远是那段英文提示 ——
    "没被吃掉"和"被吃掉"都会按同一段文本比较，校验恒真。
    """
    bash = m.resolve_bash()
    assert bash, "未找到可用的 bash"
    tmp = Path(__file__).resolve().parent / "_fixture_roundtrip.sh"
    tmp.write_text(line + "\n", encoding="utf-8")
    try:
        r = subprocess.run(
            [bash, "-c", '. "$1"; printf %s "$' + var + '"', "_", str(tmp)],
            capture_output=True,
            text=True,
        )
        return r.stdout
    finally:
        tmp.unlink(missing_ok=True)


def test_unquoted_windows_path_is_mangled():
    """反例：不引号时反斜杠被吃掉——这正是 run90 的失败形态。"""
    got = _roundtrip(f"export OF_SKILL_A={WIN_A}", "OF_SKILL_A")
    assert got != WIN_A
    # 且后端按这个被吃掉的路径查不到说明文件，报的正是那句让人误判的错
    assert "\\" not in got


def test_export_line_preserves_backslashes():
    """正例：加了单引号后必须逐字回读。"""
    got = _roundtrip(m.export_line("OF_SKILL_A", Path(WIN_A)), "OF_SKILL_A")
    assert got == WIN_A


def test_export_line_survives_single_quote_in_value():
    """值里含单引号时不得截断，也不得让整行语法失效。"""
    got = _roundtrip(m.export_line("V", Path(r"D:\it's\x")), "V")
    assert got == r"D:\it's\x"


def test_verify_via_shell_flags_mangled_file(tmp_path):
    """守卫函数本身：被吃掉的文件必须被点名，正确文件必须放行。"""
    bad_file = tmp_path / "bad.sh"
    bad_file.write_text(f"export V={WIN_A}\n", encoding="utf-8")
    bad = m.verify_via_shell(bad_file, {"V": Path(WIN_A)})
    assert bad and "V" in bad[0]

    good_file = tmp_path / "good.sh"
    good_file.write_text(m.export_line("V", Path(WIN_A)) + "\n", encoding="utf-8")
    assert m.verify_via_shell(good_file, {"V": Path(WIN_A)}) == []


def test_mangled_value_is_not_an_existing_dir():
    """导出校验的"值必须是已存在目录"这一判定，抓得住被吃掉的路径。

    只判非空抓不到：被吃掉的值仍然非空，于是校验通过而后端拿到另一个路径。
    """
    got = _roundtrip(f"export OF_SKILL_A={WIN_A}", "OF_SKILL_A")
    assert got, "被吃掉的值仍然非空——这正是只判非空会漏掉的原因"
    assert not Path(got).is_dir()

    ok = _roundtrip(m.export_line("OF_SKILL_A", Path(WIN_A)), "OF_SKILL_A")
    # 加了引号后回读一致；此处的 WIN_A 是合成路径，故只断言未被吃掉
    assert ok == WIN_A


def test_resolve_bash_rejects_wsl_launcher(monkeypatch):
    """反例：只找得到 WSL 启动器时必须判为不可用。

    否则校验会拿那段英文提示当 shell 输出，比对恒不成立/恒成立，
    看不出任何问题。
    """
    monkeypatch.setattr(
        shutil, "which", lambda name: r"C:\Windows\System32\bash.exe"
    )
    assert m.resolve_bash() == ""


def test_resolve_bash_accepts_git_bash(monkeypatch):
    monkeypatch.setattr(
        shutil, "which", lambda name: r"C:\Program Files\Git\usr\bin\bash.exe"
    )
    assert m.resolve_bash().endswith("bash.exe")
