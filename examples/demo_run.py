"""Tiny helper: write files with content (test/demo)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.llm.client import LLMClient
from omegaforge.core.budget import TokenBank
from omegaforge.agent.super_agent import SuperAgent
from omegaforge.distill.genome import Genome


def demo() -> None:
    """Run a distilled genome directly (mock mode OK)."""
    g = Genome(
        name="OmegaResearcher",
        mission_one_liner="research any topic into a tight, sourced brief",
        source_fingerprint="demo",
        persona_genes=["rigorous", "cites sources", "concise"],
        tool_genes=["web_search(query)"],
        workflow_genes=["search", "verify", "brief"],
        system_prompt="You are a rigorous research agent. Search, verify, "
                      "cite, answer under 800 words.",
        tools=["web_search"])
    agent = SuperAgent(g, LLMClient(), TokenBank(50_000))
    res = agent.run("Research: EU AI Act impact on SMEs")
    print(json.dumps({"ok": res.ok, "tokens": res.tokens_used,
                      "steps": res.steps_executed,
                      "answer": res.answer[:200]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    demo()
