#!/usr/bin/env python3
"""对照组（Baseline）的**补救通道可达性**守卫。

## 背景

`build_baseline(signals, provided_prompt="")` 的优先级是
"用户显式提供 > 源 agent 原始 提示词 > 朴素兜底"。三种 kind 中
简化对照是**不可比**的——赢一个不存在的对手不构成"更强"。

## 验证到的两处失效

**① 补救通道在生产里不存在。**
`provided_prompt` 只有测试传得进来：引擎里是 `build_baseline(sig)`，
从不转发；CLI / server / MCP 三个入口都没有对应入参。于是当源材料里
没有可提取的提示词时，对照组**必然**降级为简化对照、结论必然不成立，
而新加的 可信度说明 却写着"请提供源 Agent 原始 系统提示词"
——一条**无处可填**的指引。与"MCP 说需要确认却没有确认通道"
同型：给了指引却不给通路，用户只能原地重试。

**② 过短的对照 prompt 能洗白结论。**
`provided` 分支直接 return，不判长度：

  build_baseline(sig, provided_prompt="hi")
    -> kind="provided", comparable=True

一个 2 字符的对照组就能让结论成立标记变成真，等于给"可验证地更强"
开了自己给自己发证的口子。而同源的 `source_prompt` 分支有 120 字符下限
——两把尺子，只严了自动提取的那条。

## 守卫分层（互相兜底必须打破）

  A 单元    过短降级 / 够格可比。只测这一层抓不到"接通断了"。
  B 引擎接通  distill(baseline_prompt=...) 必须真的改变 baseline_kind。
         入口都传了、引擎不转发的话，A 仍全绿而功能仍是死的。
  C 三入口    CLI 有 --baseline-提示词、MCP schema 有该字段、
          HTTP 端到端产物 baseline_kind 变 provided。
  D 防处理过头   够格的 提示词 必须是 provided 且可比；
          源材料自带 提示词 时不得被 provided 覆盖以外的逻辑干扰。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
  sys.path.insert(0, REPO)

# 一段真实代码：够长，但本身不是 提示词 —— 对照组应降级为 简化对照
THIN_SOURCE = """import os

def process_items(items):
  results = []
  for item in items:
    results.append(item.strip())
  return results

class Pipeline:
  def run(self, data):
    return process_items(data)
"""

GOOD_PROMPT = (
  "You are ArxivResearcher, a meticulous academic research assistant. "
  "Your mission: locate and summarize academic papers with rigor and "
  "precision. Always cite sources with arXiv IDs. Never fabricate DOIs "
  "or page numbers. Verify every claim against at least one primary "
  "source before asserting it."
)


def _env(home):
  return dict(os.environ, PYTHONPATH=REPO, OMEGAFORGE_HOME=home,
        OMEGAFORGE_MOCK="1")


class BaselineUnitTest(unittest.TestCase):
  """A 层：过短不能洗白，够格才可比。"""

  def setUp(self):
    self.tmp = tempfile.mkdtemp(prefix="of_base_unit_")
    self.home = os.path.join(self.tmp, "home")
    os.environ["OMEGAFORGE_HOME"] = self.home
    os.environ["OMEGAFORGE_MOCK"] = "1"
    from omegaforge.distill.loader import SourceAgentLoader
    from omegaforge.domain.baseline import build_baseline
    self.sig = SourceAgentLoader().load(THIN_SOURCE)
    self.build = build_baseline

  def tearDown(self):
    shutil.rmtree(self.tmp, ignore_errors=True)

  def test_short_provided_cannot_whiten_verdict(self):
    b = self.build(self.sig, provided_prompt="hi")
    self.assertEqual(b.kind, "naive")
    self.assertFalse(b.comparable)
    self.assertIn("不足", b.note)

  def test_qualified_provided_is_comparable(self):
    """防处理过头：不能因为防洗白就把正常的对照一起打死。"""
    b = self.build(self.sig, provided_prompt=GOOD_PROMPT)
    self.assertEqual(b.kind, "provided")
    self.assertTrue(b.comparable)

  def test_no_prompt_falls_back_to_naive(self):
    b = self.build(self.sig)
    self.assertEqual(b.kind, "naive")
    self.assertFalse(b.comparable)

  def test_short_boundary(self):
    """120 字符是两把尺子的同一个刻度。"""
    from omegaforge.domain.baseline import MIN_PROMPT_CHARS
    just_under = "A" * (MIN_PROMPT_CHARS - 1)
    just_over = "A" * MIN_PROMPT_CHARS
    self.assertFalse(self.build(self.sig,
                  provided_prompt=just_under).comparable)
    self.assertTrue(self.build(self.sig,
                  provided_prompt=just_over).comparable)


class EngineWiringTest(unittest.TestCase):
  """B 层：引擎必须真的把 baseline_提示词 转发给 build_baseline。

  只测 A 层是不够的：入口都传了参数、引擎不转发，A 仍全绿而功能仍死。
  """

  def setUp(self):
    self.tmp = tempfile.mkdtemp(prefix="of_base_eng_")
    self.home = os.path.join(self.tmp, "home")
    os.environ["OMEGAFORGE_HOME"] = self.home
    os.environ["OMEGAFORGE_MOCK"] = "1"
    from omegaforge.core.budget import TokenBank
    from omegaforge.distill.engine import DistillEngine
    from omegaforge.llm.client import LLMClient
    self.eng = DistillEngine(LLMClient(), TokenBank(500_000),
                 arena_rounds=1, max_generations=1,
                 verbose=False)

  def tearDown(self):
    shutil.rmtree(self.tmp, ignore_errors=True)

  def test_distill_forwards_baseline_prompt(self):
    _g, rep = self.eng.distill(
      THIN_SOURCE, output_dir=os.path.join(self.tmp, "out"),
      baseline_prompt=GOOD_PROMPT)
    self.assertEqual(rep.baseline_kind, "provided")
    self.assertTrue(rep.baseline_comparable)

  def test_without_baseline_prompt_still_naive(self):
    """防处理过头：不传时行为不变，仍是朴素对照。"""
    _g, rep = self.eng.distill(
      THIN_SOURCE, output_dir=os.path.join(self.tmp, "out2"))
    self.assertEqual(rep.baseline_kind, "naive")
    self.assertFalse(rep.baseline_comparable)


class EntryPointTest(unittest.TestCase):
  """C 层：三个入口都得有这个入参——漏一个就是那条路上无处可填。"""

  def setUp(self):
    self.tmp = tempfile.mkdtemp(prefix="of_base_ep_")
    self.home = os.path.join(self.tmp, "home")
    os.environ["OMEGAFORGE_HOME"] = self.home
    os.environ["OMEGAFORGE_MOCK"] = "1"

  def tearDown(self):
    shutil.rmtree(self.tmp, ignore_errors=True)

  def test_cli_has_baseline_prompt_option(self):
    r = subprocess.run(
      [sys.executable, "-m", "omegaforge.cli", "distill", "--help"],
      capture_output=True, text=True, env=_env(self.home),
      cwd=REPO, timeout=60)
    txt = (r.stdout or "") + (r.stderr or "")
    # 必须用"完整选项名"匹配，不能用子串。
    # （回退校验校验点 C 未抓到）：把选项名改成 --baseline-提示词-X
    # 后，子串 "baseline-提示词" 依然命中，测试全绿而选项已经没了。
    self.assertRegex(txt, r"--baseline-prompt(?![-\w])",
             "CLI distill 缺少 --baseline-prompt 选项")

  def test_mcp_schema_has_baseline_prompt(self):
    from omegaforge.mcp_server import McpCore
    resp = McpCore().handle(
      {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = (resp or {}).get("result", {}).get("tools", [])
    dt = [t for t in tools if t.get("name") == "omegaforge_distill"]
    self.assertTrue(dt, "未找到 omegaforge_distill")
    props = dt[0].get("inputSchema", {}).get("properties", {})
    self.assertIn("baseline_prompt", props)

  def test_http_end_to_end_becomes_comparable(self):
    from omegaforge import server
    from omegaforge.server import Handler
    jid = Handler._start_distill({"source_prompt": THIN_SOURCE,
                   "baseline_prompt": GOOD_PROMPT})
    job = jid.get("job", "")
    self.assertTrue(job)
    for _ in range(200):
      st = server.JOBS.get(job, {}).get("status")
      if st in ("done", "succeeded", "failed", "error"):
        break
      time.sleep(0.5)
    kind = ""
    for root, _dirs, files in os.walk(self.home):
      if "report.json" in files and job in root:
        with open(os.path.join(root, "report.json"),
             encoding="utf-8") as f:
          kind = json.load(f).get("baseline_kind", "")
        break
    self.assertIn(kind, ("source_prompt", "provided"),
           f"产物 baseline_kind={kind or '未读到'}")


if __name__ == "__main__":
  unittest.main(verbosity=2)
