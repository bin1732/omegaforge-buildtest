"""OmegaForge end-to-end pipeline test (offline, MOCK mode).

Runs the full distill pipeline against a sample agent source file with
no API key: proves ingest -> extract -> compress -> synthesize -> eval
-> arena -> artifacts, plus budget accounting.
Run: python -m pytest tests/ -q   (or)  python tests/test_pipeline.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.llm.client import LLMClient
from omegaforge.core.budget import TokenBank, BudgetExceeded
from omegaforge.distill.engine import DistillEngine
from omegaforge.agent.super_agent import SuperAgent

SAMPLE_AGENT = '''
class ResearchAgent:
  """A research assistant agent."""
  name = "DeepResearcher"
  role_prompt = """You are a meticulous research assistant.
  Your goal is to research any topic and produce a structured brief.
  Persona: rigorous, cites sources, concise."""
  tools = ["web_search"]
  def workflow(self, topic):
    # step 1: search
    results = self.web_search(topic)
    # step 2: analyze sources
    # step 3: verify claims
    # finally: report
    return self.report(results)
'''


def test_full_pipeline(tmp_path) -> None:
  # ：缺少该约束时用的 tmp_dir 不是 pytest 内置 fixture，导致该用例在
  # `pytest tests/` 下长期处于 error 状态却无人发现（单独 python 跑才 PASS）。
  # 改用内置的 tmp_path（pathlib.Path），保证 CI 里真的被执行。
  tmp_dir = str(tmp_path)
  src = os.path.join(tmp_dir, "sample_agent.py")
  with open(src, "w", encoding="utf-8") as f:
    f.write(SAMPLE_AGENT)

  llm = LLMClient()           # no key -> mock mode
  assert llm.mock_mode
  bank = TokenBank(200_000)
  engine = DistillEngine(llm, bank, verbose=True)

  genome, report = engine.distill(src, output_dir=os.path.join(tmp_dir, "out"))

  # artifacts exist
  for fn in ("genome.json", "report.json", "system_prompt.md"):
    p = os.path.join(tmp_dir, "out", fn)
    assert os.path.exists(p), f"missing artifact {fn}"

  # genome valid
  g2 = Genome_load(os.path.join(tmp_dir, "out", "genome.json"))
  assert g2.name and g2.system_prompt
  assert g2.lineage.startswith("distill-of:")
  assert g2.arena_generation >= 1

  # verdict sane
  assert report.verdict in {"win", "tie", "loss", "budget-exhausted"}
  assert report.final_score >= 0

  # budget ledger consistent
  assert bank.total_spent > 0
  assert bank.total_spent <= bank.budget
  phases = bank.ledger_by_phase()
  assert any(k.startswith("distill:") for k in phases)

  # super agent can run a task from the genome
  agent = SuperAgent(g2, llm, TokenBank(50_000))
  res = agent.run("Research: impact of EU AI Act on SMEs", phase="agent")
  assert res.ok, res.error
  assert res.answer
  print(" agent answer head:", res.answer[:90].replace("\n", " "))

  print("✅ test_full_pipeline passed")


def Genome_load(p: str):
  from omegaforge.distill.genome import Genome
  return Genome.load(p)


def test_budget_hard_stop() -> None:
  bank = TokenBank(1000)
  try:
    bank.charge("x", "m", 800, 500)
    raise AssertionError("expected BudgetExceeded")
  except BudgetExceeded:
    pass
  print("✅ test_budget_hard_stop passed")


def test_envelope_validation() -> None:
  from omegaforge.core.message import Envelope, MessageBus
  bus = MessageBus()
  got = []
  bus.subscribe("writer", lambda e: got.append(e))
  env = Envelope(sender="manager", recipient="writer", kind="task",
          subject="draft", body={"text": "hi"},
          schema_hint={"text": "str"})
  bus.publish(env)
  assert len(got) == 1
  bad = Envelope(sender="m", recipient="writer", kind="task",
          subject="s", body={}, schema_hint={"text": "str"})
  try:
    bus.publish(bad)
    raise AssertionError("expected ValueError")
  except ValueError:
    pass
  print("✅ test_envelope_validation passed")


if __name__ == "__main__":
  tmp = tempfile.mkdtemp(prefix="omegaforge_test_")
  try:
    test_budget_hard_stop()
    test_envelope_validation()
    test_full_pipeline(tmp)
    print("\nALL TESTS PASSED (mock mode, offline)")
  finally:
    shutil.rmtree(tmp, ignore_errors=True)
