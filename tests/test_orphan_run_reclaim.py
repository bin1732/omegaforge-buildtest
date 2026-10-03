"""孤儿任务归位守卫。

场景：蒸馏在后台线程里跑，进程关闭或崩溃时线程随之消失，磁盘上的
事件流停在中途、没有终态事件。重启后用户打开运行记录，看到的是一个
"还在进行中"的任务，界面一直轮询、永远等不到结果。

验证（重启前后各起一个独立进程）：
 缺少该约束时 重启后读到 status=queued、phase=extract、4 条事件、2 条日志
     —— 状态停在"排队中"，与"已经跑到抽取阶段"的实际进度矛盾
 加上该约束后 重启后读到 status=interrupted

要点：
 · 归位依据是"本进程有没有在跑它"，而不是文件时间戳——时间戳要靠
  超时阈值猜，猜不准；进程内活跃集合是确定的。
 · 详情（get）与列表（list）必须同一条规则。列表走轻量路径只读
  run.json，不做归位的话两个页面会对同一个任务给出不同结论。
 · 正在跑的任务绝不能被误判：起任务时 mark_active、结束时
  clear_active，二者必须成对出现。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.core.run import TERMINAL, RunStore, is_active # noqa: E402

# 子进程：写一段"进行中"的事件流然后退出，不写终态
CHILD = textwrap.dedent("""
  import os, sys, json
  sys.path.insert(0, {root!r})
  from omegaforge.core.run import RunStore
  st = RunStore()
  r = st.create("demo", budget=50000, rounds=1, gens=1, task="中断演示")
  r.log("开始抽取")
  r.phase_to("extract", message="进入阶段 extract")
  r.log("正在处理")
  print(r.id)
""")

READER = textwrap.dedent("""
  import os, sys, json
  sys.path.insert(0, {root!r})
  from omegaforge.core.run import RunStore
  st = RunStore()
  r = st.get({rid!r})
  s = r.state()
  print(json.dumps({{
    "status": s.get("status"),
    "phase": s.get("phase"),
    "progress": s.get("progress"),
    "list_status": [x.get("status") for x in st.list(50)
            if x.get("id") == {rid!r}],
    "events": len(r.events()),
  }}, ensure_ascii=False))
""")


def _child(code: str, home: str) -> str:
  p = subprocess.run([sys.executable, "-c", code], capture_output=True,
            text=True, timeout=120, cwd=str(ROOT),
            env={**os.environ, "OMEGAFORGE_HOME": home})
  assert p.returncode == 0, p.stderr[-400:]
  return p.stdout.strip().splitlines()[-1]


def _make_orphan(home: str) -> str:
  return _child(CHILD.format(root=str(ROOT)), home)


def test_orphan_run_becomes_interrupted():
  """重启后停在中途的任务必须落到终态，不能永远"进行中"。"""
  with tempfile.TemporaryDirectory() as home:
    os.environ["OMEGAFORGE_HOME"] = home
    try:
      rid = _make_orphan(home)
      # 读在另一个进程里做：本进程若持有活跃登记就会误判
      out = _child(READER.format(root=str(ROOT), rid=rid), home)
      d = json.loads(out)
      assert d["status"] == "interrupted", d
      assert d["status"] in TERMINAL
    finally:
      os.environ.pop("OMEGAFORGE_HOME", None)


def test_list_and_detail_agree():
  """列表与详情必须对同一个任务给出同一个结论。"""
  with tempfile.TemporaryDirectory() as home:
    os.environ["OMEGAFORGE_HOME"] = home
    try:
      rid = _make_orphan(home)
      out = _child(READER.format(root=str(ROOT), rid=rid), home)
      d = json.loads(out)
      assert d["list_status"] == [d["status"]], d
    finally:
      os.environ.pop("OMEGAFORGE_HOME", None)


def test_orphan_progress_is_terminal_not_zero():
  """中断任务的进度应落在终态档位，不能退回 0 让用户以为要重跑。"""
  with tempfile.TemporaryDirectory() as home:
    os.environ["OMEGAFORGE_HOME"] = home
    try:
      rid = _make_orphan(home)
      out = _child(READER.format(root=str(ROOT), rid=rid), home)
      d = json.loads(out)
      assert d["progress"] == 100, d
    finally:
      os.environ.pop("OMEGAFORGE_HOME", None)


def test_active_run_is_not_mistaken_for_orphan():
  """防处理过头：正在跑的任务绝不能被判成中断。"""
  with tempfile.TemporaryDirectory() as home:
    os.environ["OMEGAFORGE_HOME"] = home
    try:
      st = RunStore()
      r = st.create("live", budget=50000, rounds=1, gens=1)
      from omegaforge.core.run import clear_active, mark_active
      mark_active(r.id)
      try:
        got = st.get(r.id)
        assert got.state()["status"] != "interrupted"
        assert is_active(r.id) is True
      finally:
        clear_active(r.id)
      # 注销之后，它就成了孤儿 —— 归位必须立刻生效
      assert st.get(r.id).state()["status"] == "interrupted"
    finally:
      os.environ.pop("OMEGAFORGE_HOME", None)


def test_server_marks_active_runs():
  """接线守卫：起任务与结束必须成对登记/注销。

  只测 RunStore 的归位逻辑是不够的——它不知道谁在跑。真正的接通在
  server 的执行函数里；那里漏了 mark_active，正在跑的任务会被判成
  孤儿，界面立刻显示"已中断"，而任务其实还在跑。
  """
  src = (ROOT / "omegaforge" / "server.py").read_text(encoding="utf-8")
  assert "mark_active(" in src, "起任务时未登记活跃运行"
  assert "clear_active(" in src, "任务结束时未注销活跃运行"
  # 注销必须在 finally 里：异常路径下也要注销，否则失败的任务会一直
  # 被当成"在跑"，重启前永远显示进行中。
  i = src.find("finally:")
  assert i > 0 and src.find("clear_active(", i) > i, \
    "注销未放在 finally 中，异常路径会漏掉"
