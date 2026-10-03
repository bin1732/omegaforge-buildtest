"""检查脚本：对照组（Baseline）的补救通道是否真实可达。

## 背景

`domain/baseline.py` 定义了三种对照：

| kind          | 可比性 |
|---------------|--------|
| source_prompt | 真实可比 |
| provided      | 真实可比 |
| 简化对照         | **不可比**，结论不成立 |

`build_baseline(signals, provided_prompt="")` 的优先级是
"用户显式提供 > 源 agent 原始 prompt > 朴素兜底"。

## 要验的问题

相关部分给 `可信说明` 补了"对照组不可比"的原因，文案是
"请提供源 Agent 原始 system prompt"。但如果**没有任何入口能传这个 prompt**，
那句指引就是死路——与相关部分"MCP 说需要确认却没有确认通道"同型。

本检查脚本检验：
  1. 源材料无可提取 prompt 时，是否必然降级为 简化对照 且结论不成立
  2. CLI / HTTP / MCP 三个入口，是否存在任何一个能传入对照 prompt
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))


# 一段真实代码：够长，但本身不是 prompt —— 对照组应降级为 简化对照
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


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="of_baseline_")
    home = os.path.join(tmp, "home")
    os.environ["OMEGAFORGE_HOME"] = home
    os.environ["OMEGAFORGE_MOCK"] = "1"

    from omegaforge.core.budget import TokenBank
    from omegaforge.distill.engine import DistillEngine
    from omegaforge.domain.baseline import build_baseline
    from omegaforge.distill.loader import SourceAgentLoader
    from omegaforge.llm.client import LLMClient

    print("\n[1] 源材料无可提取 prompt → 必然降级")
    sig = SourceAgentLoader().load(THIN_SOURCE)
    b = build_baseline(sig)
    check("kind=naive", b.kind == "naive", b.kind)
    check("comparable=False", b.comparable is False)
    check("降级说明含不可比警示",
          "不可比" in b.note or "不构成" in b.note, b.note[:50])

    print("\n[1b] 过短的对照 prompt 不能洗白结论")
    b2 = build_baseline(sig, provided_prompt="hi")
    check("2 字符对照降级为 naive", b2.kind == "naive", b2.kind)
    check("2 字符对照不可比", b2.comparable is False)
    b3 = build_baseline(sig, provided_prompt=GOOD_PROMPT)
    check("够格的对照才是 provided", b3.kind == "provided", b3.kind)
    check("够格的对照可比", b3.comparable is True)

    print("\n[2] 端到端跑一次蒸馏，看结论与给出的原因")
    llm = LLMClient()
    eng = DistillEngine(llm, TokenBank(500_000), arena_rounds=1,
                        max_generations=1, verbose=False)
    _g, rep = eng.distill(THIN_SOURCE, output_dir=os.path.join(tmp, "out"))
    check("baseline_kind=naive", rep.baseline_kind == "naive",
          rep.baseline_kind)
    check("baseline_comparable=False", rep.baseline_comparable is False)
    check("claim_valid=False", rep.claim_valid is False)
    note = getattr(rep, "trust_note", "") or ""
    check("trust_note 提示用户去提供原始 prompt",
          "system prompt" in note or "原始 prompt" in note, note[:60])

    print("\n[3] 三个入口能否传入对照 prompt")
    # --- CLI ---
    env = dict(os.environ, PYTHONPATH=ROOT, OMEGAFORGE_HOME=home,
               OMEGAFORGE_MOCK="1")
    r = subprocess.run([sys.executable, "-m", "omegaforge.cli", "distill",
                        "--help"], capture_output=True, text=True, env=env,
                       cwd=ROOT, timeout=60)
    cli_txt = (r.stdout or "") + (r.stderr or "")
    cli_hit = "baseline-prompt" in cli_txt or "baseline_prompt" in cli_txt
    check("CLI 有对照 prompt 入参", cli_hit,
          "已出现 --baseline-prompt" if cli_hit
          else "CLI --help 未出现 baseline 相关入参")

    # --- MCP ---
    from omegaforge.mcp_server import McpCore
    mcp = McpCore()
    resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = (resp or {}).get("result", {}).get("tools", [])
    dt = [t for t in tools if t.get("name") == "omegaforge_distill"]
    props = (dt[0].get("inputSchema", {}).get("properties", {}) if dt else {})
    check("MCP 有对照 prompt 入参",
          any("baseline" in k for k in props),
          f"MCP distill 入参={list(props)}")

    # --- HTTP ---
    from omegaforge.server import Handler
    from omegaforge import server
    payload = {"source_prompt": THIN_SOURCE, "baseline_prompt": GOOD_PROMPT}
    try:
        jid = Handler._start_distill(dict(payload))
        accepted = True
        job = jid.get("job", "")
    except Exception as e:
        accepted = False
        job = f"{type(e).__name__}: {e}"
    check("HTTP 接受 baseline_prompt", accepted, job)

    print("\n[4] 若入口接受了，是否真的改变了对照组")
    if accepted:
        import time
        for _ in range(200):
            st = server.JOBS.get(job, {}).get("status")
            if st in ("done", "succeeded", "failed", "error"):
                break
            time.sleep(0.5)
        kind = ""
        for root, _dirs, files in os.walk(home):
            if "report.json" in files and job in root:
                with open(os.path.join(root, "report.json"),
                          encoding="utf-8") as f:
                    kind = json.load(f).get("baseline_kind", "")
                break
        check("传入后 baseline_kind 变为可比",
              kind in ("source_prompt", "provided"),
              f"实际 kind={kind or '未读到产物'}")

    print(f"\n结果: PASS={len(PASS)}  FAIL={len(FAIL)}")
    for n in FAIL:
        print(f"  FAILED: {n}")
    return 1 if FAIL else 0


# 一段够长、够格的对照 prompt（>120 字符）
GOOD_PROMPT = (
    "You are ArxivResearcher, a meticulous academic research assistant. "
    "Your mission: locate and summarize academic papers with rigor and "
    "precision. Always cite sources with arXiv IDs. Never fabricate DOIs "
    "or page numbers. Verify every claim against at least one primary "
    "source before asserting it."
)


def _report_paths(home: str, job: str):
    base = os.path.join(home, "runs")
    return [os.path.join(base, job, "report.json"),
            os.path.join(base, job, "distill", "report.json")]


if __name__ == "__main__":
    sys.exit(main())
