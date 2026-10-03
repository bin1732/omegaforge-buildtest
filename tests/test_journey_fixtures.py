"""旅程 fixtures 用例。

守的是一件事：fixtures 给出的路径，Python 自己必须看得见，且说明文件
必须真的在里面。

为什么必须由 Python 生成路径：runner 上 shell 是 Git Bash，它的 /tmp 与
Windows 版 Python 看到的 /tmp 不是同一个地方。shell 建好的目录后端根本
看不见，界面报「缺少技能说明文件」——症状像"安装功能坏了"（run77）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_journey_fixtures.py"


def _run(tmp_path: Path) -> tuple[int, dict]:
    out = tmp_path / "fx.sh"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", str(tmp_path / "base"), "--out", str(out)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    env = {}
    if out.is_file():
        for line in out.read_text(encoding="utf-8").splitlines():
            body = line.strip()
            # 只收带 export 的行：不带 export 的赋值不传给子进程，node 读
            # process.env 拿不到，界面报「请先运行夹具脚本」——夹具明明刚
            # 跑过，症状与没生成完全一样。
            if not body.startswith("export "):
                continue
            body = body[len("export "):]
            if "=" in body:
                k, v = body.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return r.returncode, env


def test_paths_are_visible_to_python(tmp_path: Path):
    rc, env = _run(tmp_path)
    assert rc == 0
    for key in ("OF_JOURNEY_HOME", "OF_SKILL_A", "OF_SKILL_B", "OF_JOURNEY_BASE"):
        assert key in env, f"fixtures 未输出 {key}"
        assert os.path.isdir(env[key]), f"{key} 指向的目录不存在：{env[key]}"
    for key in ("OF_SKILL_A", "OF_SKILL_B"):
        assert os.path.isfile(os.path.join(env[key], "SKILL.md"))


def test_skill_names_are_legal(tmp_path: Path):
    """技能名只允许小写字母/数字/连字符/下划线，否则安装必被拒。"""
    rc, env = _run(tmp_path)
    assert rc == 0
    for key in ("OF_SKILL_A", "OF_SKILL_B"):
        md = Path(env[key], "SKILL.md").read_text(encoding="utf-8")
        name = [l for l in md.splitlines() if l.startswith("name:")][0]
        value = name.split(":", 1)[1].strip()
        assert value.replace("-", "").replace("_", "").isalnum() and value.islower(), value
