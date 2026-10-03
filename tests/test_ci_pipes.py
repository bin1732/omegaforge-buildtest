#!/usr/bin/env python3
"""CI 管道退出码保护检查的守卫。

## 管的是什么

`set -o pipefail` 这类保护一旦被删，CI 步骤会在被观测命令失败时仍然
报绿，而日志里看得出失败——两侧结论不一致，且不一致是静默的。本文件
保证检查本身能发现删除行为。

## 三条断言的分工

 · 有保护时放行、删掉保护时报错：确认检查真的在起作用
 · 注释里出现管道写法不算违规：确认扫描的是真实调用而非说明文字
 · 显式放弃退出码视为有表态：确认采集类步骤不会被误判
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_ci_pipes.py"
WF = ROOT / ".github" / "workflows" / "build.yml"


def run(text: str) -> tuple[int, str]:
    # 临时文件走系统临时目录：写死 /tmp 在 Windows 上不存在，5 条用例会
    # 全部以 FileNotFoundError 失败——看着像守卫逻辑坏了，实际是路径不通。
    fd, name = tempfile.mkstemp(suffix=".yml", prefix="_wf_probe_")
    os.close(fd)
    tmp = Path(name)
    tmp.write_text(text, encoding="utf-8")
    p = subprocess.run([sys.executable, str(SCRIPT), str(tmp)],
                       cwd=str(ROOT), capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def guarded(text: str, on: bool) -> str:
    # 缩进须与 steps 下的真实层级一致（6 空格）：步骤切分按缩进取正文，
    # 层级不对会让正文为空，检查对空正文恒放行。
    body = "          set -o pipefail\n" if on else ""
    return f"""      - name: X
        shell: bash
        run: |
{body}          python s.py 2>&1 | tee d.log
""" + text


def test_guarded_passes() -> None:
    rc, _ = run(guarded("", True))
    assert rc == 0, "有 pipefail 时不应报错"


def test_removed_guard_fails() -> None:
    rc, out = run(guarded("", False))
    assert rc == 1, "删掉 pipefail 后必须报错"
    assert "X" in out, "报错要点名是哪个步骤"


def test_pipestatus_counts_as_guard() -> None:
    text = """      - name: Y
        shell: bash
        run: |
          cargo build 2>&1 | tee d.log
          exit ${PIPESTATUS[0]}
"""
    rc, _ = run(text)
    assert rc == 0, "显式取首段退出码同样是保护"


def test_comment_pipe_not_flagged() -> None:
    text = """      - name: Z
        shell: bash
        run: |
          # 文档示例：cmd | tee out.log
          python s.py
"""
    rc, _ = run(text)
    assert rc == 0, "注释中的管道写法不是真实调用"


def test_explicit_waive_counts_as_declaration() -> None:
    text = """      - name: W
        shell: bash
        run: |
          { echo hi; } | tee d.log
          exit 0
"""
    rc, _ = run(text)
    assert rc == 0, "显式声明不阻断同样是表态"


def test_real_workflow_clean() -> None:
    p = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT),
                       capture_output=True, text=True)
    assert p.returncode == 0, f"本仓工作流应通过：{p.stdout}"
    assert WF.exists(), "工作流文件必须存在，否则检查对象为空仍会判通过"


def test_unparsable_workflow_is_not_clean() -> None:
    """认不出任何步骤时不能判"干净"。

    空文件、缩进层级变了、步骤结构换了都会走到没有步骤这一步，而它与
    "所有步骤都合规"在输出上完全一样。不单列这一条，守卫就会退化成恒真。
    """
    rc, out = run("")
    assert rc != 0, "空工作流不该判通过"
    assert "步骤" in out, out
