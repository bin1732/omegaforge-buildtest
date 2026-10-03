"""智能体工具调用链守卫：模型意图必须真正传到工具，写操作不得被散文误触发。

为什么单独一个文件
------------------
SuperAgent 是蒸馏产物的**运行时**——Genome 编译出来之后就是它在跑。
工具调用是它唯一的行动通道，之前这条链上有两个叠加的缺陷：

  A. 参数恒为 {"query": 整段任务}
    无论模型想传什么，工具只收到整段任务。于是 task_add 拿到
    text=""、memory_remember 拿到 fact=""——写工具必然空写或报错。
    验证：task_add 抛 ValueError("task text required")。

  B. 散文提及即触发，且带副作用
    模型写"我本来可以用 task_add 但现在不需要"，工具照样真跑一次。
    验证确认触发了真实调用。这不是误报，是用户数据被写了他没要的记录。

两者叠加的后果是"想做的做不成（A），没说要做的反而做了（B）"。

修复按**风险分级**收口，与门禁层同一条原则：风险越高越要明确。
 - 只读工具：保持宽松匹配（改它会打断弱模型的既有可用路径）
 - 写工具：必须 name(k=v) 明确调用式，否则不执行并提示纠正

本文件刻意保留"只读工具散文仍能触发"的断言——那既是既有契约，
也是有意为之：弱模型确实只会用散文表达意图。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.core.budget import TokenBank     # noqa: E402
from omegaforge.distill.genome import Genome     # noqa: E402
from omegaforge.agent.super_agent import SuperAgent  # noqa: E402


class _FakeLLM:
  model_main = model_fast = model_judge = "fake"

  def __init__(self, script):
    self.script = script
    self.i = 0

  def chat_messages(self, messages, model=None, temperature=0.4,
           max_tokens=2048, json_mode=False):
    t = self.script[min(self.i, len(self.script) - 1)]
    self.i += 1
    return type("R", (), {"text": t, "model": "fake",
               "prompt_tokens": 10, "completion_tokens": 10})()

  def chat(self, system, user, model=None, temperature=0.4,
       max_tokens=2048, json_mode=False):
    return self.chat_messages([])


def _run(script, tool_genes, tool_impls=None, task="明天下午三点开会",
     max_steps=8):
  g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
        system_prompt="你是助手", tool_genes=tool_genes)
  ag = SuperAgent(g, _FakeLLM(script), TokenBank(200000),
          tool_impls=tool_impls, max_steps=max_steps)
  return ag.run(task)


class TestArgsReachTool(unittest.TestCase):
  """A：模型想传的值必须真的传进去。"""

  def test_explicit_kwargs_passed(self):
    calls = []
    _run(['我来 task_add(text="买牛奶")', "已记录"], ["task_add(text)"],
       {"task_add": lambda a: calls.append(a) or "ok"})
    self.assertTrue(calls, "显式调用没有触发工具")
    self.assertEqual(json.loads(calls[0]).get("text"), "买牛奶",
             "模型写的参数没传进去，工具仍收到别的东西")

  def test_positional_maps_to_declared_param(self):
    calls = []
    _run(["task_add(买牛奶)", "已记录"], ["task_add(text)"],
       {"task_add": lambda a: calls.append(a) or "ok"})
    self.assertEqual(json.loads(calls[0]).get("text"), "买牛奶",
             "位置参数没有映射到声明的参数名")

  def test_priority_number_not_string(self):
    calls = []
    _run(['task_add(text="交报告", priority=1)', "已记录"],
       ["task_add(text)"],
       {"task_add": lambda a: calls.append(a) or "ok"})
    self.assertEqual(json.loads(calls[0]).get("priority"), 1,
             "数字被当成字符串，下游 int() 可能炸")

  def test_read_tool_keeps_existing_contract(self):
    """只读工具保持既有契约：散文提及即触发，query 为整段任务。"""
    calls = []
    _run(["TOOL kb_search", "查到了"], ["kb_search(query)"],
       {"kb_search": lambda a: calls.append(a) or "{}"})
    self.assertTrue(calls, "只读工具的散文触发被改坏了")
    self.assertEqual(json.loads(calls[0]).get("query"), "明天下午三点开会")


class TestWriteToolsNeedExplicitCall(unittest.TestCase):
  """B：会写数据的工具，不能被一句散文就真跑一次。"""

  def test_prose_mention_does_not_execute_write(self):
    calls = []
    _run(["我本来可以用 task_add(text) 但现在不需要", "好的"],
          ["task_add(text)"],
          {"task_add": lambda a: calls.append(a) or "ok"})
    self.assertEqual(calls, [],
             "模型只是提到了工具，写操作就被真跑了一次")

  def test_negation_still_no_side_effect(self):
    calls = []
    _run(["不需要 memory_remember(fact)，直接回答", "好的"],
       ["memory_remember(fact)"],
       {"memory_remember": lambda a: calls.append(a) or "ok"})
    self.assertEqual(calls, [], "明确说不需要，仍然写入了记忆")

  def test_explicit_call_on_write_still_executes(self):
    """收口不是禁掉写工具：明确调用必须照常执行。"""
    calls = []
    _run(['memory_remember(fact="用户偏好中文")', "记住了"],
       ["memory_remember(fact)"],
       {"memory_remember": lambda a: calls.append(a) or "ok"})
    self.assertTrue(calls, "明确调用被挡住了，写工具等于废了")

  def test_model_gets_correction_hint(self):
    """不能静默跳过：否则模型会重复同一句话直到步数耗尽。"""
    g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
          system_prompt="s", tool_genes=["task_add(text)"])
    seen = []

    class L(_FakeLLM):
      def chat_messages(self, messages, model=None, temperature=0.4,
               max_tokens=2048, json_mode=False):
        seen.append(list(messages))
        return _FakeLLM.chat_messages(self, messages)
    ag = SuperAgent(g, L(["我要 task_add 一下", "算了"]), TokenBank(50000))
    ag.run("x")
    # 必须验"提示说了什么"，只验"出现过工具名"是假守卫——
    # 把 nudge 换成静默 skip 时它照样通过（回退校验未抓到）。
    joined = json.dumps(seen, ensure_ascii=False)
    self.assertIn("会修改数据", joined,
           "模型没收到纠正提示：它不知道为什么没执行、该怎么改")


class TestNoEval(unittest.TestCase):
  """参数解析绝不能求值——模型输出是不可信输入。"""

  def test_expression_is_treated_as_text(self):
    """即使解析器将来放宽到能匹配嵌套括号，也只能是字符串。"""
    from omegaforge.agent.super_agent import _literal
    v = _literal("__import__('os').system('id')")
    self.assertEqual(v, "__import__('os').system('id')",
             "参数被求值了，等于给了模型一个 exec 通道")

  def test_malformed_call_does_not_crash(self):
    calls = []
    res = _run(['task_add(text="未闭合', "算了"], ["task_add(text)"],
          {"task_add": lambda a: calls.append(a) or "ok"})
    self.assertTrue(res is not None, "畸形调用把整个 run 打挂了")

  def test_huge_arg_is_capped(self):
    from omegaforge.agent.super_agent import _literal, _ARG_MAX
    self.assertLessEqual(len(_literal("x" * 100000)), _ARG_MAX,
               "超长参数没有上限，内存可被单条输出撑爆")
