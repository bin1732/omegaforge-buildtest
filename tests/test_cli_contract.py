"""CLI 契约守卫 —— 真实子进程跑命令，只看退出码与用户可见输出。

为什么必须用子进程：CLI 的失败信号是**退出码**，而退出码只有真正
`sys.exit(main())` 才观察得到；读代码只看得到 return，会得出"已经处理了"
的错觉。本套件每一条都跑真实进程。

覆盖的验证问题（均为真机复现，非读代码推断）：

1. 数值闸门绕过：`--rounds 0 --gens 0` 在服务端被拒、在 CLI 被放行，
  跑出 0.00/10 的空产物且 exit=0 —— 两套常量各写一份导致漂移。
2. 静默成功：`task done ""` 打印 completed: False 却返回 0；
  `report <空目录>` 什么都不打印也返回 0。脚本/CI 完全拿不到失败信号。
3. 脱敏层在 CLI 整体失效：`user_error()` 把完整 traceback 写进内部日志，
  前提是日志已接到文件；CLI 从未调用 configure_logging，于是走
  logging 的 lastResort handler 打到 stderr —— 连同本机绝对路径。
4. 日志目录不跟随 OMEGAFORGE_HOME：写死相对路径，桌面端运行目录不可控。
5. 英文文案泄漏："text required" / "path required"。
6. 甩锅模型：损坏的 genome 被说成"模型返回了无法解析的内容"。

每一条都做过回退校验（把实现退回旧行为，测试必须变红），不是形式化。
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


@pytest.fixture
def home(tmp_path):
  h = str(tmp_path / "wf")
  os.makedirs(h, exist_ok=True)
  return h


def cli(args, home, timeout=120):
  """真实跑一条 CLI 命令，返回 (退出码, 合并输出)。"""
  env = dict(os.environ)
  env["PYTHONPATH"] = ROOT
  env["OMEGAFORGE_HOME"] = home
  env["OMEGAFORGE_API_KEY"] = ""    # 强制 mock，绝不真联网
  env.pop("OMEGAFORGE_BASE_URL", None)
  p = subprocess.run([PY, "-m", "omegaforge.cli", *args],
            cwd=ROOT, env=env, capture_output=True,
            text=True, timeout=timeout)
  return p.returncode, (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------- 1. 数值闸门不再绕过
@pytest.mark.parametrize(
  "flag,label",
  [("--rounds", "评测轮次"), ("--gens", "进化代数")],
)
def test_cli_gate_rejects_zero_rounds_and_gens(home, flag, label):
  rc, out = cli(["distill", "你是一个 helpful 助手", flag, "0",
          "--out", str(os.path.join(home, "o1"))], home)
  assert rc != 0, f"{flag} 0 必须非零退出，实际 exit=0（静默成功）"
  assert label in out
  assert "distilled" not in out, "被拒就不该跑出产物摘要"


def test_cli_gate_rejects_tiny_budget(home):
  rc, out = cli(["distill", "你是一个 helpful 助手", "--budget", "0",
          "--out", str(os.path.join(home, "o2"))], home)
  assert rc != 0
  assert "预算" in out
  # 判据必须是"闸门先拦下"，而不是"跑起来之后才失败"：预算为 0 时引擎
  # 同样会以预算耗尽退出，退出码与提示里都带"预算"二字，只验这两个条件
  # 会把闸门撤掉的情形判成通过。
  assert "[DistillEngine]" not in out, "闸门未拦下，已跑进引擎"


def test_cli_defaults_match_server_constants():
  """默认值必须来自同一份常量——两边各写一份正是漂移的根源。"""
  from omegaforge.core import limits
  from omegaforge import server
  assert server.DEFAULT_BUDGET == limits.DEFAULT_BUDGET
  assert server.MIN_BUDGET == limits.MIN_BUDGET
  assert server.MAX_BUDGET == limits.MAX_BUDGET


# --------------------------------------------------- 2. 静默成功必须消失
def test_task_done_empty_is_not_silent_success(home):
  rc, out = cli(["task", "done", ""], home)
  assert rc != 0, "task done 空参数返回 0 = 用户与脚本都以为完成了"
  assert "请指定要完成的任务名称" in out


def test_report_missing_dir_is_not_silent_success(home):
  rc, out = cli(["report", str(os.path.join(home, "nope_zzz"))], home)
  assert rc != 0, "report 空目录什么都不打印却返回 0"
  assert "report.json" in out


def test_run_missing_genome_is_not_silent_success(home):
  rc, out = cli(["run", str(os.path.join(home, "nope.json")), "任务"], home)
  assert rc != 0
  assert "找不到该 genome 文件" in out


# --------------------------------------- 3. 脱敏层：栈与路径绝不进用户通道
def test_no_traceback_leaks_to_user_output(home):
  """损坏文件触发异常时，用户可见输出里不得出现任何栈或绝对路径。"""
  bad = os.path.join(home, "bad.json")
  with open(bad, "w", encoding="utf-8") as f:
    f.write("{ this is not json")
  rc, out = cli(["run", bad, "任务"], home)
  assert rc != 0
  assert "Traceback" not in out, "完整 Python 栈泄漏到用户可见输出"
  assert "user-visible-error-suppressed" not in out, "内部日志被打到 stdout/stderr"
  assert ".py" not in out, f"泄漏了模块路径：{out[:200]}"
  assert ROOT not in out, "泄漏了本机绝对路径"


def test_user_error_never_leaks_traceback_even_unconfigured(home):
  """脱敏层自己兜底，不依赖每个入口记得调用 configure_logging。

  缺少该约束时：CLI 从未调用 configure_logging，logging 走 lastResort
  把完整 traceback 打到 stderr。与其要求每个入口都记得接通（已漏过一次），
  不如让脱敏层惰性兜底——这条守卫视的是"忘了接通也不许漏"。
  """
  env = dict(os.environ)
  env["PYTHONPATH"] = ROOT
  env["OMEGAFORGE_HOME"] = home
  code = ("from omegaforge.core.errors import user_error\n"
      "m = user_error(RuntimeError('boom-internal-detail'))\n"
      "print('MSG:', m)\n")
  p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=120)
  out = (p.stdout or "") + (p.stderr or "")
  assert "MSG:" in out
  assert "Traceback" not in out, "脱敏层把栈吐到了控制台"
  assert "boom-internal-detail" not in out, "内部异常原文泄漏"
  assert "user-visible-error-suppressed" not in out


def test_corrupt_genome_message_does_not_blame_model(home):
  """缺少该约束时：损坏 genome 被说成"模型返回了无法解析的内容"——
  这是读用户文件失败，跟模型无关，会误导用户去查接口。"""
  bad = os.path.join(home, "bad.json")
  with open(bad, "w", encoding="utf-8") as f:
    f.write("{ this is not json")
  rc, out = cli(["run", bad, "任务"], home)
  assert rc != 0
  assert "模型返回了无法解析的内容" not in out
  assert "genome" in out


def test_corrupt_report_json_is_reported_as_file_problem(home):
  d = os.path.join(home, "out")
  os.makedirs(d, exist_ok=True)
  with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
    f.write("{ broken")
  rc, out = cli(["report", d], home)
  assert rc != 0
  assert "损坏" in out
  # 判据是"点名了出问题的文件"，而不是"全文不许出现模型二字"——后者会
  # 误伤任何合法提到模型的提示（例如未配置模型服务的演示模式提示）。
  assert "report.json" in out, f"报错未点名出问题的文件：{out}"
  assert "模型返回了无法解析的内容" not in out


# --------------------------------------------------- 4. 用户可见文案须中文
@pytest.mark.parametrize(
  "args,english",
  [(["task", "add", ""], "text required"),
   (["skill", "install", ""], "path required")],
)
def test_no_english_strings_leak(home, args, english):
  rc, out = cli(args, home)
  assert rc != 0
  assert english not in out, f"英文文案泄漏：{english}"


# ------------------------------------------- 5. 内部日志跟随数据目录
def test_error_log_follows_data_home(tmp_path, monkeypatch):
  """缺少该约束时：configure_logging 写死相对路径，完全不看
  OMEGAFORGE_HOME —— 用户设了统一数据目录，唯独落着 traceback 的
  错误日志仍留在当前工作目录（桌面端运行目录不可控）。"""
  h = str(tmp_path / "wf")
  monkeypatch.setenv("OMEGAFORGE_HOME", h)
  from omegaforge.core.errors import default_log_dir
  assert default_log_dir() == os.path.join(h, "logs")


def test_configure_logging_never_blocks_startup(tmp_path):
  """日志目录不可写时只降级，绝不抛——日志是旁路，不该成为启动失败的原因。"""
  from omegaforge.core import errors
  blocked = str(tmp_path / "readonly")
  os.makedirs(blocked, exist_ok=True)
  os.chmod(blocked, 0o500)
  try:
    # root 下权限位可能拦不住，故直接用一个必然无法创建的路径形态
    p = errors.configure_logging(os.path.join(blocked, "x", "y"))
    assert isinstance(p, str)
  finally:
    os.chmod(blocked, 0o700)


def test_configure_logging_returns_empty_on_uncastable_dir():
  """把目录路径构造成一个文件 —— makedirs 必然失败，必须返回空串而非抛出。"""
  from omegaforge.core import errors
  with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
    path = f.name
  try:
    assert errors.configure_logging(path) == ""
  finally:
    os.unlink(path)


# ------------------------------------- 6. 第三个入口：MCP stdio 同样不许漏
def test_mcp_stderr_never_carries_traceback(home):
  """MCP 是第三个入口，缺少该约束时同样从未调用 configure_logging。

  它比 CLI 更危险：stdout 是协议通道（不能污染），stderr 常被 host
  （Claude Desktop 等）原样采集进日志面板——traceback 连同本机路径会
  直接出现在**别人的客户端**里。
  """
  d = os.path.join(home, "adir")
  os.makedirs(d, exist_ok=True)
  msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
          "clientInfo": {"name": "p", "version": "1"}}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "fs_read", "arguments": {"path": d}}},
  ]
  env = dict(os.environ)
  env["PYTHONPATH"] = ROOT
  env["OMEGAFORGE_HOME"] = home
  env["OMEGAFORGE_API_KEY"] = ""
  p = subprocess.run([PY, "-m", "omegaforge.mcp_server"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            cwd=ROOT, env=env, capture_output=True,
            text=True, timeout=180)
  assert "Traceback" not in p.stderr, "MCP 把栈吐到了 stderr（host 会采集）"
  assert "user-visible-error-suppressed" not in p.stderr


def test_mcp_error_log_lands_in_data_home(home):
  """内部日志必须落在 OMEGAFORGE_HOME/logs，而不是当前工作目录。"""
  # 只发 initialize，不发任何会出错的请求：出错会让转译层的惰性兜底
  # 顺手把日志目录建出来，那样无论入口有没有显式接通都成立，这条断言就
  # 验不到接线了。判据因此是"启动即落盘"。
  msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
          "clientInfo": {"name": "p", "version": "1"}}},
  ]
  env = dict(os.environ)
  env["PYTHONPATH"] = ROOT
  env["OMEGAFORGE_HOME"] = home
  env["OMEGAFORGE_API_KEY"] = ""
  subprocess.run([PY, "-m", "omegaforge.mcp_server"],
          input="".join(json.dumps(m) + "\n" for m in msgs),
          cwd=ROOT, env=env, capture_output=True,
          text=True, timeout=180)
  assert os.path.isdir(os.path.join(home, "logs")), "日志未落到数据目录"
