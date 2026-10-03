"""用户视角三处「不可行动提示」的守卫与反验。

为什么需要
------------
以下三处的共同点是「有提示、但照着做也没用」：

1. 技能安装填了不存在的路径 → 「未找到对应记录」。用户既不知道缺的是
   SKILL.md，也不知道该核对路径，只会以为应用坏了。
2. 工具名填错 → 提示里只给展示名（「运行终端命令」）。使用者照提示填
   会再次被拒，而错误信息一模一样——人或模型都无法自行纠正，形成死循环。
3. 比对被结论闸门挡住 → 只说「有一侧的结论本身不成立」。用户不知道卡在
   哪，于是反复重试，而重试一百次结果都一样。

三处都必须给出**可行动**的提示。本文件同时给出反验：把修复撤掉，用例
必须变红——否则说明用例没碰到那条路径。
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from omegaforge.distill.engine import DistillEngine  # noqa: E402
from omegaforge.skills.manager import SkillManager  # noqa: E402
from omegaforge.tools.system_tools import tool_dispatch  # noqa: E402


# ------------------------------------------------------------------ 1 技能


def _install_msg(tmp_path) -> str:
    mgr = SkillManager(str(tmp_path / "skills"))
    try:
        mgr.install(str(tmp_path / "no-such-dir"))
    except Exception as e:
        return str(e)
    return ""


def test_skill_install_missing_path_is_actionable(tmp_path):
    msg = _install_msg(tmp_path)
    assert msg, "没抛异常，等于静默成功"
    assert "技能说明文件" in msg, f"未说清缺什么：{msg}"
    assert ("路径" in msg or "目录" in msg), f"未指向可核对的对象：{msg}"
    assert "未找到对应记录" not in msg, f"仍是兜底句，用户无从行动：{msg}"


def test_skill_install_missing_path_reverse(tmp_path):
    """反验：改回抛 FileNotFoundError 时，提示必须重新变成不可行动。

    这条守卫存在的意义就是防止有人把它改回 FileNotFoundError——那会被
    统一转译层压成「未找到对应记录」，而任何只查"有没有抛异常"的用例
    都照样通过。
    """
    # 只看 install() 里真实的 raise 语句：按字面量扫会命中注释里提到的
    # FileNotFoundError（那段注释正是在解释这个坑），于是守卫会在正确
    # 实现上误报——而人的第一反应是把它整条删掉，守卫反而消失。
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(SkillManager.install).lstrip())
    raised = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            fn = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            raised.add(getattr(fn, "id", None) or getattr(fn, "attr", None))
    assert "FileNotFoundError" not in raised, \
        f"install 又把 FileNotFoundError 抛了出去，用户会看到「未找到对应记录」：{raised}"
    assert "UserError" in raised, \
        f"缺 SKILL.md 时应抛 UserError 直接说清缺什么：{raised}"


# ------------------------------------------------------------------ 2 工具名


def test_unknown_tool_lists_real_identifiers(tmp_path):
    try:
        tool_dispatch("运行终端命令", {}, home=str(tmp_path))
    except Exception as e:
        msg = str(e)
    else:
        raise AssertionError("填错工具名却没被拒")
    # 必须给出真正可提交的标识，否则照提示填会再次被拒
    for ident in ("run_command", "fs_read", "fs_write", "fs_list", "web_fetch"):
        assert ident in msg, f"提示缺少可提交标识 {ident}：{msg}"
    assert "标识" in msg, f"未说明该填哪一部分：{msg}"


def test_unknown_tool_reverse():
    """反验：提示里若只有展示名，用例必须失败。

    只列展示名的版本同样会「抛异常、有提示」，看起来完全正常——唯一的
    症状是使用者永远填不对。所以这条必须单独盯。
    """
    src = open(os.path.join(REPO, "omegaforge", "tools", "system_tools.py"),
               encoding="utf-8").read()
    assert "请填写括号前的标识" in src, \
        "修复被撤掉了：提示须同时给出标识与展示名"


def test_known_tool_dispatchable(tmp_path):
    """真实标识必须真能进到执行层（不是只出现在提示里）。"""
    home = str(tmp_path)
    try:
        tool_dispatch("fs_list", {"path": "."}, home=home)
    except Exception as e:
        msg = str(e)
        # 未授权也是"进到了执行层"；只有"没有名为…的工具"才是没接上
        assert "没有名为" not in msg, f"真实标识却接不上执行层：{msg}"


# ------------------------------------------------------------------ 3 比对


def _blocked_reason() -> str:
    prev = {"final_score": 6.0, "eval_set_fingerprint": "abc12345",
            "baseline_comparable": True, "claim_valid": False}
    curr = {"final_score": 7.0, "eval_set_fingerprint": "abc12345",
            "baseline_comparable": True, "claim_valid": False}
    return DistillEngine.compare_reports(prev, curr)["reason"]


def test_compare_blocked_reason_is_actionable():
    reason = _blocked_reason()
    assert reason, "拒绝却没给理由"
    # 只说"不成立"不够：用户不知道卡在哪，只会反复重试
    assert "评审" in reason or "模型" in reason, \
        f"未指出卡在哪一环：{reason}"
    assert "重新蒸馏" in reason or "配置" in reason, \
        f"未给出可行动指引：{reason}"


def test_compare_still_computes_delta_when_valid():
    """闸门放行时必须真算出差值——否则整段只是在拒绝。"""
    prev = {"final_score": 6.0, "eval_set_fingerprint": "abc12345",
            "baseline_comparable": True, "claim_valid": True}
    curr = {"final_score": 7.5, "eval_set_fingerprint": "abc12345",
            "baseline_comparable": True, "claim_valid": True}
    out = DistillEngine.compare_reports(prev, curr)
    assert out["comparable"] is True, f"条件齐备却仍不可比：{out}"
    assert out["delta"] == 1.5, f"差值算错：{out}"


# ------------------------------------------------------- 4 命令行本地子命令


def _run_cli(argv, capsys):
    from omegaforge.cli import main
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, out


def test_cli_local_cmd_has_no_demo_notice(tmp_path, capsys, monkeypatch):
    """纯本地命令不该先来一句"没配模型"。

    `kb list` 列出的是磁盘上真实存在的条目，与模型无关。带上那句提示，
    用户会以为清单不可信，还会去做一个对本命令毫无作用的设置。
    """
    monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))
    rc, out = _run_cli(["kb", "list"], capsys)
    assert rc == 0, f"本地命令不该失败：{out}"
    assert "演示模式" not in out, f"本地命令却报演示模式：{out[:200]}"


def test_cli_task_stats_is_human_readable(tmp_path, capsys, monkeypatch):
    """统计不许把 Python 字面量打到终端上。"""
    monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))
    _run_cli(["task", "add", "买牛奶"], capsys)
    rc, out = _run_cli(["task", "list"], capsys)
    assert rc == 0
    assert "{'" not in out, f"输出里出现了字典字面量：{out[:200]}"
    assert "共" in out and "待办" in out, f"统计不可读：{out[:200]}"


def test_cli_demo_notice_still_shown_for_distill(tmp_path, capsys):
    """反验：真正会调模型的命令仍须提示——不能为了不误报而整条删掉。"""
    import inspect
    from omegaforge import cli
    src = inspect.getsource(cli._dispatch)
    assert ('args.cmd in ("distill", "run")' in src
            or "args.cmd in ('distill', 'run')" in src), \
        "提示被整条删掉了：真正会调模型的命令也需要这句提示"
