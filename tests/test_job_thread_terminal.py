"""任务线程的终态守卫。

场景：蒸馏跑在后台线程里。线程的**准备阶段**（构造客户端、初始化记账器、
写第一条日志）原先整段位于错误处理之外——它同样会失败（读配置、写磁盘）。
一旦失败，线程直接死亡：

  · 活跃登记不清空 → 本进程一直认为"它还在跑"
  · 事件流没有终态 → 重启前永远识别不出这是中断任务
  · 界面状态保持"运行中"，进度条不动，也不给任何报错

用户看到的就是点了蒸馏之后一直转圈，且不提示任何原因；只有退出并重启
应用之后，这条任务才会被识别为中断。

为什么不能用"等超时"来兜底：超时阈值靠猜，猜长了界面白等、猜短了会误杀
正常长任务。正确判据是确定的——线程结束时必须落终态、必须清空登记。

守卫分三层：
  1. 准备阶段失败也要走错误处理（不能漏在 try 之外）
  2. 错误处理自身失败时仍有兜底终态（finally 里补）
  3. 防处理过头：正常成功的任务不受影响，且终态不被覆盖
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.core.run import TERMINAL, is_active # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
  h = tmp_path / "home"
  h.mkdir()
  monkeypatch.setenv("OMEGAFORGE_HOME", str(h))
  import omegaforge.server as srv
  from omegaforge.core import run as run_mod
  srv.RUNS.home = None      # 让单例跟随环境变量
  run_mod._ACTIVE.clear()
  yield srv
  run_mod._ACTIVE.clear()


def _start(srv, run, source="demo", budget=2000, rounds=1, gens=1, task="t"):
  t = threading.Thread(
    target=srv._run_job,
    args=(run, source, budget, rounds, gens, run.dir, task, None, ""),
    daemon=True)
  t.start()
  t.join(timeout=20)
  return t


def test_setup_phase_failure_still_lands_terminal(home, monkeypatch):
  """准备阶段出错：必须落到终态，而不是卡在"运行中"。"""
  srv = home

  class Boom(Exception):
    pass

  def bad_client(*a, **k):
    raise Boom("准备阶段失败")

  monkeypatch.setattr(srv, "LLMClient", bad_client)
  run = srv.RUNS.create("demo", budget=2000, rounds=1, gens=1, task="t")
  _start(srv, run)

  assert not is_active(run.id), "线程已结束，活跃登记必须清空"
  assert srv.JOBS[run.id]["status"] != "running", "不能停在 running"
  fresh = srv.RUNS.get(run.id)
  assert fresh.status in TERMINAL, f"应为终态，实际 {fresh.status}"


def test_error_handler_failure_still_lands_terminal(home, monkeypatch):
  """错误处理自身也失败时，finally 的兜底必须补上终态。"""
  srv = home

  def bad_user_error(*a, **k):
    raise RuntimeError("错误处理自身失败")

  monkeypatch.setattr(srv, "user_error", bad_user_error)
  run = srv.RUNS.create("demo", budget=2000, rounds=1, gens=1, task="t")
  _start(srv, run)

  assert not is_active(run.id)
  fresh = srv.RUNS.get(run.id)
  assert fresh.status in TERMINAL, f"应为终态，实际 {fresh.status}"
  assert srv.JOBS[run.id]["status"] != "running"

  # 终态必须真正**写进事件流**，不能只靠读取时推断。
  #
  # 这两者互相掩盖：读取侧会把"本进程没在跑且无终态"的任务推断成中断，
  # 于是不写终态也照样显示正确 —— 单撤兜底时所有状态断言仍然全绿。
  # 但事件流本身停在半路：日志区不会给出任何说明，任何直接读事件流的
  # 消费方（导出、跨版本比对、命令行）拿到的都是一个没有结论的任务。
  evs = fresh.events()
  last_phase = evs[-1].get("phase") if evs else None
  assert last_phase in TERMINAL, \
    f"事件流末条不是终态：{last_phase}（阶段序列 {[e.get('phase') for e in evs]}）"


def test_successful_run_is_not_overwritten(home, monkeypatch):
  """防处理过头：正常成功的任务终态为 succeeded，不被兜底改成中断。"""
  srv = home

  class Report:
    verdict = "win"
    final_score = 8.0
    baseline_score = 6.0
    generation = 1
    baseline_kind = "extracted"
    baseline_comparable = True
    baseline_note = ""
    claim_valid = True
    eval_set_fingerprint = "abc123"
    eval_set_cases = 3

  class FakeEngine:
    def __init__(self, *a, **k):
      self._log = None

    def distill(self, *a, **k):
      return object(), Report()

  monkeypatch.setattr(srv, "DistillEngine", FakeEngine)
  run = srv.RUNS.create("demo", budget=2000, rounds=1, gens=1, task="t")
  _start(srv, run)

  fresh = srv.RUNS.get(run.id)
  assert fresh.status == "succeeded", f"成功不应被覆盖，实际 {fresh.status}"
  assert not is_active(run.id)


def test_ensure_terminal_does_not_rewrite_existing_terminal(home):
  """已有终态时 ensure_terminal 必须原样不动。"""
  srv = home
  run = srv.RUNS.create("demo", budget=2000, rounds=1, gens=1, task="t")
  run.succeed(final_score=1.0)
  before = run.events()[-1]
  assert run.ensure_terminal("补终态") is False
  assert run.events()[-1] == before, "已有终态不应再写事件"
  assert run.status == "succeeded"
