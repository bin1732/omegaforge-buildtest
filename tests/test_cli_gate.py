"""CLI 第二个入口的门禁守卫 —— 全部走真实 subprocess，不测内层函数。

为什么坚持走子进程：前几轮反复吃亏在"只测内层方法、接通漏了测试也全绿"。
CLI 的失败形态恰恰只在进程边界上才成立（退出码、stderr、traceback 泄漏），
在进程内调用 main() 根本看不到这些。

每条守卫都对应一处验证修复，撤回修复必须变红（见 probes/rev_cli_gate.py）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cli(*args, timeout=60, home=None):
  """真实跑一次 CLI，返回 (退出码, 合并输出)。"""
  env = dict(os.environ)
  env.pop("OMEGAFORGE_API_KEY", None)     # 强制 mock，避免真实联网
  env["PYTHONPATH"] = ROOT
  env["OMEGAFORGE_HOME"] = home or tempfile.mkdtemp()
  try:
    p = subprocess.run([sys.executable, "-m", "omegaforge.cli", *args],
              capture_output=True, text=True, env=env,
              timeout=timeout, cwd=tempfile.gettempdir())
    return p.returncode, (p.stdout or "") + (p.stderr or "")
  except subprocess.TimeoutExpired:
    return "TIMEOUT", ""


def _last(out: str) -> str:
  lines = [l for l in out.strip().splitlines() if l.strip()]
  return lines[-1] if lines else ""


def _no_leak(out: str) -> bool:
  """CLI 面向用户，任何 traceback / 本机绝对路径都属于泄漏。"""
  return "Traceback" not in out and ROOT not in out


# ----------------------------------------------------------------------
# 一、畸形 genome：TypeError 曾完整甩栈（含本机路径）
# ----------------------------------------------------------------------

@pytest.mark.parametrize("body,needle", [
  ('{"hello": 1}', "不是有效的产物文件"),
  ('{"name": "a"}', "缺少必需内容"),
  ('[]', "不是有效的产物文件"),
  ('"abc"', "不是有效的产物文件"),
])
def test_genome_malformed_no_traceback(body, needle):
  """畸形产物文件不得甩栈，且必须说清是哪一类不合格。

  断言用面向用户的措辞：界面文案改过之后，测试若仍按内部字段名断言，
  就会在文案已正确、测试已过期的情况下报错——报错指向的是测试不是产品。
  """
  d = tempfile.mkdtemp()
  p = os.path.join(d, "g.json")
  with open(p, "w", encoding="utf-8") as f:
    f.write(body)
  rc, out = _run_cli("run", p, "任务")
  assert rc != 0, "畸形产物文件必须失败"
  assert _no_leak(out), f"泄漏了 traceback 或本机路径：{out[-300:]}"
  assert needle in out
  # 内部字段名不能上屏
  assert "genome" not in out.lower()


def test_genome_newer_schema_hints_upgrade():
  """文件是对的、程序旧了 —— 指引必须不同，不能笼统说"损坏"。"""
  d = tempfile.mkdtemp()
  p = os.path.join(d, "g.json")
  with open(p, "w", encoding="utf-8") as f:
    json.dump({"name": "a", "mission_one_liner": "m",
          "source_fingerprint": "f", "schema_version": "2.0"}, f)
  rc, out = _run_cli("run", p, "任务")
  assert rc != 0
  assert "升级" in out and _no_leak(out)


def test_genome_valid_still_runs():
  """防处理过头：合法 genome 不能被误杀。"""
  d = tempfile.mkdtemp()
  p = os.path.join(d, "g.json")
  with open(p, "w", encoding="utf-8") as f:
    json.dump({"name": "a", "mission_one_liner": "m",
          "source_fingerprint": "f"}, f)
  rc, out = _run_cli("run", p, "任务")
  assert rc == 0, out[-300:]
  assert _no_leak(out)


# ----------------------------------------------------------------------
# 二、数值闸门：曾绕过服务端口径，且报英文 usage
# ----------------------------------------------------------------------

@pytest.mark.parametrize("args,needle", [
  (("--budget", "abc"), "预算需要填写数字"),
  (("--rounds", "0"), "评测轮次不能小于"),
  (("--gens", "-1"), "进化代数不能小于"),
])
def test_distill_numeric_gate(args, needle):
  rc, out = _run_cli("distill", "你是一个研究助手，负责分析资料并给出结论。",
            *args, "--out", tempfile.mkdtemp())
  assert rc != 0
  assert needle in out
  assert "usage:" not in out, "不该回落到 argparse 英文 usage 转储"


@pytest.mark.parametrize("val,needle", [
  ("99", "优先级不能大于"),
  ("-5", "优先级不能小于"),
  ("abc", "优先级需要填写数字"),
])
def test_task_priority_gate(val, needle):
  rc, out = _run_cli("task", "add", "守卫任务", "--priority", val)
  assert rc != 0
  assert needle in out
  assert "usage:" not in out


def test_task_priority_valid_ok():
  rc, out = _run_cli("task", "add", "正常任务", "--priority", "2")
  assert rc == 0, out[-300:]
  assert "P2" in out


# ----------------------------------------------------------------------
# 三、静默成功与英文报错
# ----------------------------------------------------------------------

def test_kb_add_empty_chinese():
  """缺少该约束时：英文 `text required`，且走 stdout。"""
  rc, out = _run_cli("kb", "add")
  assert rc != 0
  assert "请填写要存入的内容" in out
  assert "text required" not in out


def test_wiki_save_empty_chinese():
  rc, out = _run_cli("wiki", "save")
  assert rc != 0
  assert "请同时填写词条标识" in out
  assert "required" not in out


def test_wiki_get_missing_is_not_silent_success():
  """缺少该约束时：什么都不打印且返回 0 —— 与已处理的 report <空目录> 同类。"""
  rc, out = _run_cli("wiki", "get", "definitely-not-a-slug")
  assert rc != 0, "词条不存在必须给非零退出码"
  assert "没有找到词条" in out


def test_skill_invoke_empty_distinct_from_not_found():
  """没填和填错是两回事，不能混成一句。"""
  rc, out = _run_cli("skill", "invoke")
  assert rc != 0
  assert "请填写要调用的技能名" in out
  assert "找不到对应的文件或技能" not in out


def test_report_empty_dir_not_silent():
  rc, out = _run_cli("report", "/tmp/definitely_not_here_zzz")
  assert rc != 0
  assert "没有找到 report.json" in out


# ----------------------------------------------------------------------
# 四、纯空白参数 —— kb add 早就 strip 了，其余三处没有（不一致）
# ----------------------------------------------------------------------

@pytest.mark.parametrize("args,needle", [
  (("task", "add", "  "), "请填写任务内容"),
  # 缺少该约束时：Tasks.add 抛英文 ValueError("task text required")，
  # 再被压成泛化的「请求内容有误」——用户填了空格，看到的是系统故障级模糊提示。
  (("wiki", "save", " ", "--title", " "), "词条地址只能包含"),
  # 缺少该约束时：slug 规则说明是中文，却因 ValueError 被 _classify 整句吞掉。
  (("skill", "install", "  "), "请提供技能目录路径"),
  # 缺少该约束时：报「找不到对应的文件或技能」——没填 vs 填错混成一句。
])
def test_blank_args_have_specific_message(args, needle):
  rc, out = _run_cli(*args)
  assert rc != 0
  assert needle in out, f"缺少明确提示：{out[-200:]}"
  assert "请检查后重试" not in out, "不该回落到泛化文案"
  assert _no_leak(out)


# ----------------------------------------------------------------------
# 五、--out 填成文件 / 填空 —— 曾伪装成 500 级故障
# ----------------------------------------------------------------------

def test_out_empty_is_specific():
  rc, out = _run_cli("distill", "你是一个研究助手，负责分析资料并给出结论。",
            "--out", "", "--rounds", "1", "--gens", "1")
  assert rc != 0
  assert "请指定产物输出目录" in out


def test_out_is_file_not_server_fault(tmp_path):
  """缺少该约束时：NotADirectoryError → 「操作失败，请稍后重试」。

  用户只是把输出目录填成了一个已存在的文件，却被报成服务器故障。
  """
  f = tmp_path / "a_file"
  f.write_text("x", encoding="utf-8")
  rc, out = _run_cli("distill", "你是一个研究助手，负责分析资料并给出结论。",
            "--out", str(f), "--rounds", "1", "--gens", "1")
  assert rc != 0
  assert "已存在的文件" in out
  assert "请稍后重试" not in out, "不该伪装成系统故障"
  assert _no_leak(out)


# 不可用路径按平台取：/proc 与 /dev/null 都是 POSIX 专有的，Windows 上
# 它们只是普通的不存在路径，创建会**成功**，于是用例在 Windows 上反而
# 绿得毫无意义——CL I 正常跑完，收口层一次没走到。这里的取值在每个平台
# 上都必须真的不可用，取不到就让用例失败，而不是悄悄少测一条。
_UNUSABLE_KINDS = ["parent_is_file", "reserved_or_missing"]


def _unusable_home(kind, tmp_path):
  if kind == "parent_is_file":
    # 把一个已存在的文件当目录用：两个平台都必然失败。
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    return str(f / "sub" / "deep")
  if os.name == "nt":
    # Windows：保留设备名不可作为目录名。
    return "NUL\\omegaforge-home"
  return "/proc/nope"


@pytest.mark.parametrize("kind", _UNUSABLE_KINDS)
def test_data_dir_unusable_is_chinese(kind, tmp_path):
  home = _unusable_home(kind, tmp_path)
  """数据目录不可用时：给中文，不把英文 errno 与本机路径甩到命令行上。

  收口层有四条分支（UserError / FileNotFoundError / OSError-ValueError-
  KeyError / 兜底 Exception）。若用例只停在业务层预检，业务层会先给出
  中文，收口分支一次都走不到——此时把任一条分支改成回显异常原文，用例照样
  全绿，四条分支是否真的收口便无从判断。

  因此本用例把数据目录指到不可用处，让异常真的冒泡到收口层：一类是把
  已存在的文件当目录用（必然失败），另一类是平台特有的不可用路径。

  断言用面向用户的特征（不出现英文 errno），不按具体中文文案断言：文案
  改过之后，按固定串断言会在文案已正确、测试已过期的情况下报错，报错指向
  的是测试不是产品。
  """
  rc, out = _run_cli("task", "add", "守卫任务", home=home)
  assert rc != 0, f"数据目录不可用时必须失败：{out[-300:]}"
  assert "[Errno" not in out, f"异常原文上屏：{out[-300:]}"
  assert _no_leak(out), f"泄漏了 traceback 或本机路径：{out[-300:]}"
  assert re.search(r"[\u4e00-\u9fff]", out), f"失败时没有给中文说明：{out[-300:]}"


def test_out_valid_dir_still_works(tmp_path):
  """防处理过头：正常目录不能被误杀。"""
  d = tmp_path / "outdir"
  rc, out = _run_cli("distill", "你是一个研究助手，负责分析资料并给出结论。",
            "--out", str(d), "--rounds", "1", "--gens", "1")
  assert rc == 0, out[-400:]
  assert (d / "genome.json").exists()


def main() -> int:
  return pytest.main([__file__, "-q"])


if __name__ == "__main__":
  # 顶层 sys.exit 会让 pytest 在收集阶段整个崩溃（0 用例可跑），
  # 这个坑本项目已经踩过三次，必须保留 main guard。
  sys.exit(main())
