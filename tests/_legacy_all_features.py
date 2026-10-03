"""OmegaForge 全功能逐项审计测试 — 每个模块的每项功能单独验证，含异常路径。

运行: python3 tests/_legacy_all_features.py
输出: 逐项 PASS/FAIL + 最终矩阵统计（任何 FAIL 都会导致非零退出码）
"""
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 环境隔离：独立数据目录，避免读到开发机上的 providers.json 等状态
os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_audit_home_")

from omegaforge.llm.client import LLMClient
from omegaforge.core.budget import TokenBank, BudgetExceeded
from omegaforge.core.message import Envelope, MessageBus, Blackboard
from omegaforge.distill.loader import SourceAgentLoader
from omegaforge.distill.genome import Genome
from omegaforge.distill.engine import DistillEngine
from omegaforge.agent.super_agent import SuperAgent
from omegaforge.core.errors import UserError
from omegaforge.cli import main as cli_main

RESULTS = []
SECTION = [""]


def check(name, fn):
  try:
    fn()
    RESULTS.append((SECTION[0], name, True, ""))
    print(f" ✅ {name}")
  except Exception as e:            # noqa: BLE001
    RESULTS.append((SECTION[0], name, False, str(e)[:120]))
    print(f" ❌ {name} → {type(e).__name__}: {str(e)[:120]}")


def section(title):
  SECTION[0] = title
  print(f"\n◆ {title}")


TMP = tempfile.mkdtemp(prefix="of_audit_")


# ═══════════════════════ 1. TokenBank ═══════════════════════
section("1. TokenBank 预算硬约束")
tb = TokenBank(10_000)
def _t11():
  tb.charge("p1", "m", 100, 50)
  assert tb.total_spent == 150, tb.total_spent
check("1.1 正常记账 charge", _t11)
def _t12():
  assert tb.ledger_by_phase().get("p1") == 150, tb.ledger_by_phase()
check("1.2 分阶段台账 ledger_by_phase", _t12)
def _t13():
  assert tb.remaining == 9_850, tb.remaining
check("1.3 remaining 计算", _t13)
def _t14():
  try:
    tb.charge("p1", "m", 9_900, 500)
    raise AssertionError("expected BudgetExceeded")
  except BudgetExceeded:
    pass
check("1.4 超预算熔断 BudgetExceeded", _t14)
def _t15():
  tb2 = TokenBank(1_000)
  try:
    tb2.charge_estimate("x", "m", 2_000)
    raise AssertionError("expected BudgetExceeded")
  except BudgetExceeded:
    pass
check("1.5 预留预检 charge_estimate", _t15)
def _t16():
  bank = TokenBank(100_000)
  def worker():
    for _ in range(50):
      bank.charge("c", "m", 10, 5)
  ts = [threading.Thread(target=worker) for _ in range(8)]
  [t.start() for t in ts]; [t.join() for t in ts]
  assert bank.total_spent == 8 * 50 * 15, bank.total_spent
check("1.6 线程安全并发记账 (8×50)", _t16)
def _t17():
  p = os.path.join(TMP, "ledger.json")
  tb.dump(p)
  d = json.load(open(p))
  assert d["summary"]["spent"] == tb.total_spent and len(d["records"]) >= 1
check("1.7 台账落盘 dump", _t17)


# ═══════════════════════ 2. 消息层 ═══════════════════════
section("2. Envelope / MessageBus / Blackboard")
def _t21():
  e = Envelope(sender="a", recipient="b", kind="task", subject="s",
         body={"x": "1"}, schema_hint={"x": "str"})
  assert e.validate() == []
check("2.1 合法 Envelope 校验通过", _t21)
def _t22():
  bad = Envelope(sender="a", recipient="b", kind="wat", subject="s", body={})
  assert bad.validate() # unknown kind 报错
check("2.2 非法 kind 被拒", _t22)
def _t23():
  bad = Envelope(sender="a", recipient="b", kind="task", subject="s",
          body={}, schema_hint={"need": "str"})
  errs = bad.validate()
  assert any("missing field" in e for e in errs)
check("2.3 schema 缺字段被拒", _t23)
def _t24():
  bad = Envelope(sender="a", recipient="b", kind="task", subject="s",
          body={"n": "x"}, schema_hint={"n": "int"})
  assert any("must be int" in e for e in bad.validate())
check("2.4 类型不符被拒", _t24)
def _t25():
  bus = MessageBus(); hits = []
  bus.subscribe("w", lambda e: hits.append("w"))
  bus.subscribe("*", lambda e: hits.append("*"))
  bus.publish(Envelope(sender="m", recipient="w", kind="task", subject="s", body={}))
  assert hits == ["w", "*"], hits
check("2.5 总bus 定向+广播订阅", _t25)
def _t26():
  bb = Blackboard()
  bb.post("k", "v1", "a"); bb.post("k", "v2", "b")
  assert bb.latest("k") == "v2" and len(bb.history("k")) == 2
  assert bb.latest("nope", "dft") == "dft" and "k" in bb.keys()
check("2.6 Blackboard 槽位/历史/默认值", _t26)


# ═══════════════════════ 3. LLMClient ═══════════════════════
section("3. LLMClient（Mock 模式全覆盖）")
llm = LLMClient()
check("3.1 无 key 自动 Mock", lambda: (_ for _ in ()).throw(AssertionError()) if not llm.mock_mode else None)
def _mock_call(prompt, **kw):
  return llm.chat("You are OmegaForge distillation core. Output ONLY valid JSON.",
          prompt, json_mode=True)
def _t32():
  r = _mock_call("As AgentArchaeologist reconstruct the SPEC. SIGNALS: {}")
  d = json.loads(r.text); assert "name" in d and "workflow" in d
check("3.2 EXTRACT 分支返回 Spec JSON", _t32)
def _t33():
  r = _mock_call("Compress this agent SPEC into a Genome. SPEC: {}")
  d = json.loads(r.text); assert "persona_genes" in d and "upgrade_genes" in d
check("3.3 COMPRESS 分支返回 Genome JSON", _t33)
def _t34():
  r = _mock_call("Compile this Genome into a production agent. system_prompt needed. GENOME: {}")
  d = json.loads(r.text); assert "system_prompt" in d
check("3.4 SYNTHESIZE 分支返回编译产物", _t34)
def _t35():
  # 出题条数不钉死。钉死条数后，任何让出题更贴合使命的改动都会被这条
  # 判失败，而这类改动正是评测有效性的前提。守住的是形态不变式与
  # 随使命变化：写死的考题会让评测集与被测能力无关。
  r = _mock_call("Design a compact exam. cases with rubric. MISSION: m")
  cases = json.loads(r.text)["cases"]
  assert cases and all(c.get("input") and c.get("rubric") for c in cases)
  r2 = _mock_call("Design a compact exam. cases with rubric. "
                  "MISSION: 审查代码并给出可落地的修改建议")
  cases2 = json.loads(r2.text)["cases"]
  assert [c["input"] for c in cases] != [c["input"] for c in cases2]
check("3.5 GEN_EVAL 分支返回可判分考卷且随使命变化", _t35)
def _t36():
  r = _mock_call("TASK: research AI trends\nRUBRIC: sourced; concise\n\nANSWER A:\nverbose baseline\n\nANSWER B:\ndistilled answer")
  d = json.loads(r.text)
  assert set(d["scores"].keys()) == {"A", "B"} and d["winner"] in {"A", "B", "tie"}
check("3.6 JUDGE 分支返回 A/B 五维分", _t36)
def _t37():
  r = _mock_call("TASK: research\nJUDGE REASON: B missed citations and ran long\nANSWER A: structured a\nANSWER B: weak b")
  d = json.loads(r.text); assert "deltas" in d and len(d["deltas"]) >= 2
check("3.7 CRITIQUE 分支返回基因突变", _t37)
def _t38():
  r1 = llm.chat("s", "hello world this is a long enough prompt")
  r2 = llm.chat("s", "hello world this is a long enough prompt")
  assert r1.text == r2.text # mock 确定性
check("3.8 Mock 确定性（同输入同输出）", _t38)
def _t39():
  c = LLMClient().configure("http://127.0.0.1:11434/v1", "",
               {"main": "qwen2.5:7b"})
  assert not c.mock_mode, "无 key 本地端点不应进 Mock"
check("3.9 无 key 本地端点走真实调用（Ollama 场景）", _t39)


# ═══════════════════════ 4. SourceAgentLoader ═══════════════════════
section("4. SourceAgentLoader 通用摄取")
loader = SourceAgentLoader()
def _t41():
  p = os.path.join(TMP, "a.py")
  open(p, "w").write('class FooAgent:\n  name="Foo"\n  role_prompt="""You are a research agent. Search then report."""\n  tools=["web_search"]')
  s = loader.load(p)
  assert s.source_type == "python" and s.name_hint == "Foo" or s.name_hint == "a"
  assert "research" in s.role_hints
check("4.1 Python 文件摄取（角色提示/命名）", _t41)
def _t42():
  s = loader.load("You are a meticulous translator agent. Always preserve tone. Step 1: read. Then translate. Finally review.")
  assert s.source_type == "text" and "translator" in s.role_hints
  assert "review" in s.workflow_cues
check("4.2 纯 prompt 文本摄取（工作流线索）", _t42)
def _t43():
  p = os.path.join(TMP, "crew.yaml")
  open(p, "w").write("name: blog_team\nagents:\n - name: writer_agent\n  role: writing posts\n  goal: draft posts\ntools:\n - name: search\n  description: web search")
  s = loader.load(p)
  assert s.source_type == "yaml" and "writer" in " ".join(s.role_hints)
check("4.3 YAML (CrewAI 风格) 摄取", _t43)
def _t44():
  p = os.path.join(TMP, "ag.json")
  open(p, "w").write(json.dumps({"name": "scout", "system_message": "You are a scouting analyst agent that scans markets.", "tools": [{"name": "web_search", "description": "search the web"}]}))
  s = loader.load(p)
  assert s.source_type == "json" and "analyst" in s.role_hints
  assert any("web_search" in t for t in s.tool_candidates)
check("4.4 JSON (AutoGen 风格) 摄取+工具候选", _t44)
def _t45():
  p = os.path.join(TMP, "persona.md")
  open(p, "w").write("# Review Agent\n## System Prompt\nYou are a meticulous code reviewer agent. You inspect diffs line by line, flag security issues, and demand tests.\n## Tools\n- read_file: reads a file")
  s = loader.load(p)
  assert s.source_type == "text" and "reviewer" in s.role_hints
  assert len(s.prompt_candidates) >= 1 and "code reviewer" in s.prompt_candidates[0]
check("4.5 Markdown 人设摄取（151k★ 生态格式）", _t45)
def _t46():
  d = os.path.join(TMP, "repo_src")
  os.makedirs(d, exist_ok=True)
  open(os.path.join(d, "agent.py"), "w").write('class PlannerAgent:\n  role_prompt="You plan tasks."\n')
  open(os.path.join(d, "README.md"), "w").write("# Planner\nA planning agent that reviews plans step by step.")
  s = loader.load(d)
  assert s.source_type == "directory" and len(s.file_list) == 2
  assert "plan" in s.workflow_cues
check("4.6 目录仓库扫描摄取", _t46)
def _t47():
  # 契约：无效输入必须抛 UserError（400 + 中文人话），不得再抛 FileNotFoundError。
  # 旧行为经 errors.py 映射成 404「未找到对应记录」，把"内容太短"报成了
  # "文件路径写错"，把人指向完全错误的方向。
  try:
    loader.load("short")
    raise AssertionError("无效输入未报错")
  except UserError as e:
    assert "太短" in str(e) or "无法识别" in str(e), f"提示不达意: {e}"
  except FileNotFoundError:
    raise AssertionError(
      "仍在用 FileNotFoundError，会被映射成 404「未找到对应记录」，方向错误")
check("4.7 无效输入正确报错", _t47)
def _t48():
  s1 = loader.load("You are a test agent for fingerprint stability check. " * 3)
  s2 = loader.load("You are a test agent for fingerprint stability check. " * 3)
  assert s1.fingerprint == s2.fingerprint and len(s1.fingerprint) == 12
check("4.8 指纹确定性 (sha1[:12])", _t48)


# ═══════════════════════ 5. Genome ═══════════════════════
section("5. Genome 基因组")
def _t51():
  g = Genome(name="X", mission_one_liner="m", source_fingerprint="f",
        persona_genes=["p1"], tool_genes=["t(a)"], workflow_genes=["w1"],
        upgrade_genes=["u1"])
  g2 = Genome.from_json(g.to_json())
  assert g2.name == "X" and g2.persona_genes == ["p1"] and g2.id == g.id
check("5.1 JSON 序列化/反序列化往返", _t51)
def _t52():
  p = os.path.join(TMP, "g.json")
  Genome(name="Y", mission_one_liner="m", source_fingerprint="f").save(p)
  assert Genome.load(p).name == "Y"
check("5.2 save/load 落盘", _t52)
def _t53():
  a = Genome(name="A", mission_one_liner="m", source_fingerprint="f",
        persona_genes=["rigorous"], tool_genes=["search(q)"],
        workflow_genes=["plan"])
  b = Genome(name="A", mission_one_liner="m", source_fingerprint="f",
        persona_genes=["rigorous", "concise"], tool_genes=["search(q)", "verify(x)"],
        workflow_genes=["plan", "execute"], upgrade_genes=["cite all"])
  d = a.diff_summary(b)
  assert d["added_persona"] == ["concise"] and d["added_tools"] == ["verify(x)"]
  assert d["added_workflow"] == ["execute"] and d["added_upgrades"] == ["cite all"]
check("5.3 基因 diff_summary（版本对比）", _t53)


# ═══════════════════════ 6. DistillEngine ═══════════════════════
section("6. DistillEngine 蒸馏引擎（七步逐一 + 进化路径）")
eng = DistillEngine(llm, TokenBank(200_000), verbose=False)
SIG = loader.load('class WriterAgent:\n  name="Scribe"\n  role_prompt="""You are a precise technical writer. Persona: clear, structured, no jargon."""\n  tools=["read_file","write_file"]')
def _t61():
  spec = eng.step_extract(SIG)
  assert spec.name and isinstance(spec.tools, list) and isinstance(spec.quality_bars, list)
check("6.1 Step2 EXTRACT: 信号→SourceSpec", _t61)
def _t62():
  spec = eng.step_extract(SIG)
  g = eng.step_compress(spec, lineage="distill-of:test", fingerprint="test")
  assert g.lineage == "distill-of:test" and g.persona_genes and g.tool_genes
  assert g.workflow_genes and g.baseline_tokens_per_task > 0
check("6.2 Step3 COMPRESS: Spec→Genome", _t62)
def _t63():
  spec = eng.step_extract(SIG)
  g = eng.step_compress(spec, "distill-of:t", "t")
  g = eng.step_synthesize(g)
  assert g.system_prompt and g.est_system_tokens > 0 and g.workflow
check("6.3 Step4 SYNTHESIZE: Genome→编译 prompt", _t63)
def _t64():
  # 与 3.5 同理：出题条数不钉死，守形态不变式与随使命变化。
  g = Genome(name="R", mission_one_liner="research with sources",
        source_fingerprint="f")
  cases = eng.step_gen_eval(g)
  assert cases and all(c.get("input") and c.get("rubric") for c in cases)
  g2 = Genome(name="R", mission_one_liner="审查代码并给出可落地的修改建议",
        source_fingerprint="f2")
  cases2 = eng.step_gen_eval(g2)
  assert [c["input"] for c in cases] != [c["input"] for c in cases2]
check("6.4 Step5 GEN_EVAL: 自动出题随使命变化", _t64)
def _t65():
  g = Genome(name="R", mission_one_liner="research",
        source_fingerprint="f",
        system_prompt="You are a rigorous research agent.",
        tool_genes=["web_search(query)"], tools=["web_search"])
  cases = eng.step_gen_eval(g)
  d, b, reasons, pairs = eng.run_arena(g, cases, 1)
  assert 0 <= d <= 10 and 0 <= b <= 10 and len(pairs) == len(cases[:eng.arena_rounds])
  assert all("winner" in p for p in pairs)
check("6.5 Step6 ARENA: 对战+裁判+战报", _t65)
def _t66():
  g0 = Genome(name="R", mission_one_liner="research", source_fingerprint="f",
        system_prompt="base")
  g1 = eng.evolve_step(g0, "task x", "B too verbose", "A answer", "B answer")
  assert len(g1.upgrade_genes) >= 2 and g1.system_prompt # 注入突变并重编译
check("6.6 Step7 EVOLVE: critique→基因突变→重编译", _t66)
class ForcedLossEngine(DistillEngine):
  """第一代强制判负，验证进化循环真实触发。"""
  def run_arena(self, g, cases, generation):
    if generation == 1:
      pairs = [{"case": cases[0]["id"], "task": cases[0]["input"],
           "answer_baseline": "structured A", "answer_distilled": "weak B",
           "score_baseline": 7.5, "score_distilled": 5.0,
           "judge_reason": "B missed citations and ran long",
           "winner": "A"}]
      return 5.0, 7.5, ["B missed citations"], pairs
    return super().run_arena(g, cases, generation)
def _t67():
  src = os.path.join(TMP, "evo_src.py")
  open(src, "w").write(SIG.raw_text)
  eng2 = ForcedLossEngine(llm, TokenBank(200_000), max_generations=3, verbose=False)
  g, rep = eng2.distill(src, output_dir=os.path.join(TMP, "evo"))
  assert rep.generation == 2, f"generation={rep.generation}"
  assert rep.verdict == "win"
  assert len(g.upgrade_genes) >= 2     # 第 1 代败后注入了突变
  gens = [h for h in g.arena_history if "gen" in h]
  assert len(gens) == 2 and gens[0]["distilled"] < gens[0]["baseline"]
  assert gens[1]["distilled"] > gens[1]["baseline"]  # 进化后反超
  for fn in ("genome.json", "report.json", "arena_pairs.json", "system_prompt.md"):
    assert os.path.exists(os.path.join(TMP, "evo", fn)), fn
check("6.7 败→进化→反超 全路径（4 产物落盘）", _t67)


# ═══════════════════════ 7. SuperAgent ═══════════════════════
section("7. SuperAgent 运行时")
class FakeResp:
  def __init__(self, text):
    self.text = text; self.prompt_tokens = 50
    self.completion_tokens = 20; self.model = "fake"
class FakeLLM:
  model_fast = "fake"; model_main = "fake"; model_judge = "fake"
  def __init__(self, script): self.script = list(script); self.calls = []
  def chat(self, system, user, model=None, temperature=0.4, max_tokens=0, json_mode=False):
    self.calls.append(user)
    return FakeResp(self.script.pop(0) if self.script else "done")
  def chat_messages(self, messages, model=None, temperature=0.4, max_tokens=0, json_mode=False):
    # SuperAgent 走多轮通道后，替身必须跟上；脚本消费顺序不变，
    # calls 仍记录" user 说了什么"，既有断言不受影响。
    self.calls.append(
      next((m.get("content", "") for m in reversed(messages)
         if m.get("role") == "user"), ""))
    return FakeResp(self.script.pop(0) if self.script else "done")
def _t71():
  g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
        system_prompt="Answer cleanly.")
  ag = SuperAgent(g, FakeLLM(["final answer 42"]), TokenBank(10_000))
  r = ag.run("do x", phase="t71")
  assert r.ok and r.answer == "final answer 42" and r.steps_executed == ["llm:0"]
check("7.1 基础问答直通", _t71)
def _t72():
  g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
        system_prompt="Use tools.", tool_genes=["web_search(query)"],
        tools=["web_search"])
  fl = FakeLLM(["let me web_search that", "FINAL: 3 key findings, cited"])
  ag = SuperAgent(g, fl, TokenBank(10_000))
  r = ag.run("research AI", phase="t72")
  assert r.ok and "tool:web_search" in r.steps_executed
  assert "tool:web_search" in ag.memory
check("7.2 工具调用循环（触发→执行→收敛）", _t72)
def _t73():
  g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
        system_prompt="Use tools.", tool_genes=["search(q)"], tools=["search"])
  fl = FakeLLM(["i will search now"] * 10)
  ag = SuperAgent(g, fl, TokenBank(10_000), max_steps=4)
  r = ag.run("go", phase="t73")
  # error 是用户可见文案（已由英文枚举改为中文），断言语义而非字面值，
  # 避免后续文案微调导致测试脆断
  assert not r.ok and "已达步数上限" in (r.error or "") \
    and len(r.steps_executed) >= 4
check("7.3 步数熔断 max_steps（防死循环）", _t73)
def _t74():
  g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
        system_prompt="Use tools.", tool_genes=["calc(x)"], tools=["calc"])
  calls = []
  fl = FakeLLM(["need calc", "FINAL: 7"])
  ag = SuperAgent(g, fl, TokenBank(10_000),
          tool_impls={"calc": lambda args: calls.append(args) or "49"})
  r = ag.run("sqrt 49", phase="t74")
  assert calls and "49" in json.dumps(calls) and r.answer == "FINAL: 7"
check("7.4 真实工具实现注入 tool_impls", _t74)
def _t75():
  g = Genome(name="T", mission_one_liner="m", source_fingerprint="f",
        system_prompt="s", tools=[{"name": "search",
                     "description": "web search",
                     "params": {"type": "object",
                           "properties": {"q": {"type": "string"}}}}])
  ag = SuperAgent(g, FakeLLM(["x"]), TokenBank(10_000))
  sch = ag.tool_schemas()
  assert sch[0]["function"]["name"] == "search" and "q" in json.dumps(sch)
check("7.5 OpenAI function-schema 导出", _t75)


# ═══════════════════════ 8. CLI ═══════════════════════
section("8. CLI 三命令")
OUT8 = os.path.join(TMP, "cli_out")
check("8.1 omegaforge distill", lambda: (_ for _ in ()).throw(AssertionError()) if cli_main(["distill", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_agent_demo.py"), "--out", OUT8]) != 0 else None)
check("8.2 omegaforge report", lambda: (_ for _ in ()).throw(AssertionError()) if cli_main(["report", OUT8]) != 0 else None)
check("8.3 omegaforge run", lambda: (_ for _ in ()).throw(AssertionError()) if cli_main(["run", os.path.join(OUT8, "genome.json"), "Research something"]) != 0 else None)


# ═══════════════════════ 9. Studio Server ═══════════════════════
section("9. Studio Server API")
def _t9():
  import threading as th
  import urllib.request
  import urllib.error
  from omegaforge import server as srv
  from http.server import ThreadingHTTPServer
  srv.JOBS.clear()
  httpd = ThreadingHTTPServer(("127.0.0.1", 8791), srv.Handler)
  th.Thread(target=httpd.serve_forever, daemon=True).start()
  base = "http://127.0.0.1:8791"
  try:
    # 9.1 status
    d = json.load(urllib.request.urlopen(base + "/api/status"))
    assert d["version"] and "mock_mode" in d
    print(" ✅ 9.1 GET /api/status")
    RESULTS.append(("9. Studio Server API", "9.1 GET /api/status", True, ""))
    # 9.2 根路径：纯 API 存活探测，且后端不再托管任何 HTML 界面
    #   （界面由 Tauri 壳内的 frontend/dist 提供，避免两套界面随包发布）
    raw = urllib.request.urlopen(base + "/").read().decode()
    assert raw.lstrip().startswith("{"), "根路径应返回 JSON，不应返回 HTML"
    assert "<html" not in raw.lower(), "后端不得托管 HTML 界面"
    d0 = json.loads(raw)
    assert d0.get("ok") is True and d0.get("service") == "omegaforge"
    print(" ✅ 9.2 GET / 仅返回存活探测（不托管界面）")
    RESULTS.append(("9. Studio Server API", "9.2 GET / 仅返回存活探测", True, ""))
    # 9.3 提交蒸馏+轮询到 done
    req = urllib.request.Request(base + "/api/distill",
                   data=json.dumps({"source": SIG.raw_text}).encode(),
                   headers={"Content-Type": "application/json"})
    jid = json.load(urllib.request.urlopen(req))["job"]
    for _ in range(40):
      jd = json.load(urllib.request.urlopen(f"{base}/api/jobs/{jid}"))
      if jd["status"] == "done":
        break
      th.TIMEOUT = None
      import time as _t; _t.sleep(0.3)
    assert jd["status"] == "done" and jd["verdict"] in {"win", "tie", "loss"}
    assert jd["genome"]["system_prompt"]
    print(" ✅ 9.3 POST /api/distill → job done（含 genome/verdict）")
    RESULTS.append(("9. Studio Server API", "9.3 POST /api/distill → job done", True, ""))
    # 9.4 未知 job 404
    try:
      urllib.request.urlopen(base + "/api/jobs/nope")
      raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
      assert e.code == 404
    print(" ✅ 9.4 未知 job → 404")
    RESULTS.append(("9. Studio Server API", "9.4 未知 job → 404", True, ""))
    # 9.5 坏 JSON → 400
    req = urllib.request.Request(base + "/api/distill", data=b"{bad",
                   headers={"Content-Type": "application/json"})
    try:
      urllib.request.urlopen(req)
      raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
      assert e.code == 400
    print(" ✅ 9.5 坏 JSON → 400")
    RESULTS.append(("9. Studio Server API", "9.5 坏 JSON → 400", True, ""))
    # 9.6 缺 source → 400
    req = urllib.request.Request(base + "/api/distill", data=b"{}",
                   headers={"Content-Type": "application/json"})
    try:
      urllib.request.urlopen(req)
      raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
      assert e.code == 400
    print(" ✅ 9.6 缺 source → 400")
    RESULTS.append(("9. Studio Server API", "9.6 缺 source → 400", True, ""))
    # 9.7 未知路径 404
    try:
      urllib.request.urlopen(base + "/api/none")
      raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
      assert e.code == 404
    print(" ✅ 9.7 未知路径 → 404")
    RESULTS.append(("9. Studio Server API", "9.7 未知路径 → 404", True, ""))
  finally:
    httpd.shutdown()
_t9()


# ═══════════════════════ 汇总 ═══════════════════════
passed = sum(1 for *_, ok, _e in RESULTS if ok)
failed = [r for r in RESULTS if not r[2]]
print("\n" + "═" * 60)
print(f"审计结果: {passed}/{len(RESULTS)} PASS")
if failed:
  print("失败项:")
  for s, n, _o, e in failed:
    print(f" ❌ [{s}] {n} → {e}")
  shutil.rmtree(TMP, ignore_errors=True)
  sys.exit(1)
print("全部功能逐项验证通过 ✅")
shutil.rmtree(TMP, ignore_errors=True)
