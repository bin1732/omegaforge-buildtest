"""SuperAgent 层契约守卫。

覆盖 agent 循环里缺少该约束时未被审计的四处：

21.1 工具名匹配必须最长优先（子串抢占）
21.2 工具输出进上下文必须有上限，且截断不得破坏边界标记
21.3 写工具必须幂等（同一调用不重复写用户数据）
21.4 写工具必须留可查审计（不是只存内存）

每项的回退校验见 tests/probes/ 下的回退脚本；守卫本身也经过
"撤掉修复是否变红"的检验，不是形式化。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.agent.super_agent import (     # noqa: E402
  SuperAgent, _tool_turn)
from omegaforge.distill.genome import Genome    # noqa: E402


def _mk(tool_genes, name="probe_agent"):
  g = Genome.__new__(Genome)
  g.name = name
  g.system_prompt = "sp"
  g.tools = []
  g.tool_genes = tool_genes
  return SuperAgent(g, None, None)


class _FakeLLM:
  """恒定输出同一句工具调用的假模型。

  为什么用恒定输出而不是"第一次调用、第二次收敛"：
  蒸馏的目标场景恰恰是弱模型，不能把"模型会收敛"当成前提。
  恒定输出正是最容易暴露重复写入问题的形态。
  """

  def __init__(self, text):
    self.model_fast = "m"
    self._text = text

  def chat_messages(self, convo, model=None, max_tokens=None):
    class R:
      text = self._text
      model = "m"
      prompt_tokens = 1
      completion_tokens = 1
    return R()


class _FakeBank:
  def charge_estimate(self, *a, **k):
    pass

  def charge(self, *a, **k):
    pass

  def ledger_by_phase(self):
    return {}


# ------------------------------------------------------------ 21.1
@pytest.mark.parametrize("genes", [
  ["add(x)", "task_add(text)"],
  ["task_add(text)", "add(x)"],
])
def test_longest_tool_name_wins(genes):
  """短名是长名的子串时，必须命中长名。

  缺少该约束时：tool_genes=['add(x)','task_add(text)']，模型明确写
  task_add(text="买牛奶")，却命中了 add、参数是整段任务——模型意图
  被整个丢弃，且执行了错误的工具。tool_genes 的声明顺序来自加载的
  Genome，不受我们控制，所以不能靠顺序碰运气。
  """
  r = _mk(genes)._decide_tool_call('请执行 task_add(text="买牛奶")', "任务")
  assert r is not None
  assert r[0] == "task_add"
  assert r[1] == {"text": "买牛奶"}


# ------------------------------------------------------------ 21.2
def test_tool_output_is_capped_and_fence_intact():
  """工具输出必须有上限，且截断后边界标记仍然闭合。

  缺少该约束时：100 万字符原样进 convo，12 步下上下文线性暴涨。
  截断必须在 taint **之前**——先包边界标记再截断会把结束标记切掉，
  内容整段落在"看起来已闭合"的边界标记之外，正是边界标记本要防的失效模式。
  """
  big = "注入内容。" * 200000
  out = _tool_turn("kb_search", big)
  assert "已截断" in out
  assert len(out) < len(big)
  # 边界标记闭合：结束标记存在，且其外无实质残留
  from omegaforge.tools.provenance import taint
  end = taint("x", "t")["text"].split("x")[-1].strip()
  assert end in out
  assert len(out.split(end)[-1].strip()) < 300


# ------------------------------------------------------------ 21.3 / 21.4
def test_write_tool_idempotent_and_audited(monkeypatch, tmp_path):
  """写工具重复调用只写一次，且留下可查审计。

  缺少该约束时：模型恒定输出同一句 kb_add，12 步里真实写入 11 条
  重复记录，且只在内存字典留痕（进程结束即丢，用户看到"被加了记录"
  却无从追溯）。
  """
  monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))
  from omegaforge.tools import policy

  sa = _mk(["kb_add(title,text)"])
  sa.llm = _FakeLLM('我来执行 kb_add(title="t", text="x")')
  sa.bank = _FakeBank()

  calls = []

  def impl(a):
    calls.append(a)
    return json.dumps({"id": len(calls)})

  sa.tool_impls["kb_add"] = impl
  res = sa.run("加一条笔记")

  assert len(calls) == 1, f"写工具被重复执行 {len(calls)} 次"
  assert any(s.startswith("dedup:") for s in res.steps_executed), \
    "重复调用应显式去重，不能静默跳过"

  aud = policy.audit_read(limit=50, tool="kb_add")
  assert len(aud) == 1
  assert aud[0].get("source") == "super_agent"
  assert aud[0].get("agent") == "probe_agent"
  assert "title" in (aud[0].get("args") or "")


def test_read_tool_not_deduped():
  """只读工具不去重——重复查询无害，且既有契约如此。

  这条是防"处理过头"：去重若扩展到只读工具，会打断弱模型反复查询
  的既有可用路径。风险分级只收紧写操作。
  """
  sa = _mk(["kb_search(query)"])
  r = sa._decide_tool_call("我需要 kb_search(query=\"折扣\")", "任务")
  assert r is not None and r[0] == "kb_search"
