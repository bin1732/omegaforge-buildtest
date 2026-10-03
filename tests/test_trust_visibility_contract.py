"""结论可信度的**可见性**守卫。

背景（本次改动验证发现）
--------------------
后端已经算出了"为什么这个结论不成立"，并且区分四种成因、各给出
可执行路径（engine.py 1521–1550 的 可信度说明）：

 - 答案含操纵痕迹 → 请检查被测方提示词
 - 裁判与作答同源 → 请把裁判配成另一个模型
 - 考题由被测方自出 → 请填自己的考题（换裁判修不掉）
 - 未完成双顺序去偏 → 可稍后重试

但前端竞技场页缺少该约束时**完全不读**结论说明字段，而是硬编码一句：

  "对照组未取到源 Agent 的原始 提示词，已降级为简化对照。"

验证误导：默认配置下结论不成立的真因是「裁判与作答同源」
（裁判自证为真），界面却让用户去换对照组——而换对照组根本
修不好自证。**给错原因比不给更糟**：用户会照着它做无效修复，
还以为已经处理过了。

同时核实到另外两处：
 - 裁判自证 / 出题侧自证在报告里是真字段，
  但没有任何 .tsx 渲染它们（只在标签表里有翻译），用户看不到。
 - 标签表里的 `contamination_note` 是**幽灵键**：全仓检索 后端
  从不产生该字段，真实键是结论说明字段。

本文件因此守三件事：
 1. 界面必须读后端权威原因（可信度说明）
 2. 幽灵键不得复活
 3. 真实蒸馏下 可信度说明 必须非空（否则界面退回硬编码文案）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _src(rel: str) -> str:
  # 注意 `src` 这一层：源码在 frontend/src/{lib,pages}/ 下，漏掉它会让
  # read_text 抛 FileNotFoundError，而失败信息长得很像"前端文件不存在"，
  # 极易被误读成产品缺陷。此处初版就漏了，三项守卫因此全红。
  p = FRONTEND / "src" / rel
  assert p.exists(), f"前端源码路径不存在（层级是否漏了 src/）：{p}"
  return p.read_text(encoding="utf-8")


def _label_keys() -> set[str]:
  """取 labels.ts 映射表的键名，先剥离注释。

  不剥离就会把注释里的反例当真——验证：幽灵键守卫一度把注释中
  "`contamination_nute` 是幽灵键"这句说明本身判为"标签表里存在幽灵键"。
  这与wiki 那次是同一类失误：字符串匹配命中的是注释而非代码。
  """
  src = _src("lib/labels.ts")
  src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
  src = re.sub(r"//[^\n]*", "", src)
  return set(re.findall(r"^\s{2}([a-z_][a-z0-9_]*)\s*:\s*['\"]",
             src, flags=re.M))


def _code(rel: str) -> str:
  """取剥离注释后的源码，用于"代码里到底做了什么"的断言。

  用全文 `in` 判断的守卫在本项目已三次出现同类假绿：匹配命中的是
  注释里的说明文字而非真实代码。本文件 B 校验点初版就是这样——
  ArenaPage.tsx 里结论说明字段首次出现在第 47 行的说明中，
  撤回时替换掉的正是注释，代码里的读取原封不动，守卫照样通过。
  """
  src = _src(rel)
  src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
  src = re.sub(r"(?m)^\s*//.*$", "", src)  # 只剥整行注释，避免误伤 URL
  return src


def test_arena_page_reads_authoritative_reason():
  """竞技场页必须读 可信度说明，而不是硬编码单一原因。

  只断言"页面上有这句中文"是不够的——那正是失效前的状态。
  必须断言它读的是后端的 可信度说明 字段。
  """
  # 必须是"代码里读取"，不是"文件里提到"。用 _code 剥注释，
  # 并断言取值表达式而非裸字段名。
  s = _code("pages/ArenaPage.tsx")
  assert re.search(r"report[?.]*\s*trust_note", s) or re.search(
    r"trust_note\s*[:?]", s), (
    "竞技场页代码里未读取 trust_note，会退回硬编码的 naive 文案，"
    "在自证/考题自出等场景下给出错误原因")


def test_no_ghost_contamination_note_key():
  """`contamination_note` 不得出现在前端或后端——它是幽灵键。

  后端真实字段是 可信度说明。幽灵键的危险在于：后人照着它去找字段、
  照着它写读取逻辑，永远读到 undefined 却不报错。
  """
  # 只查映射表的键名，不查全文：注释里允许出现这个反例（用来说明为什么
  # 它不是真字段），全文匹配会把说明本身判为缺陷。
  assert "contamination_note" not in _label_keys(), (
    "标签表里出现了幽灵键 contamination_note：后端全仓不存在该字段，"
    "真实键是 trust_note")
  # 后端侧同样剥离注释后匹配，避免注释里的说明造成误报
  hits: list[str] = []
  for p in (ROOT / "omegaforge").rglob("*.py"):
    t = p.read_text(encoding="utf-8", errors="ignore")
    t = re.sub(r'"""[\s\S]*?"""', "", t)
    t = re.sub(r"#[^\n]*", "", t)
    if "contamination_note" in t:
      hits.append(str(p.relative_to(ROOT)))
  assert not hits, f"后端出现了幽灵键 contamination_note：{hits}"


def test_trust_fields_have_labels():
  """三个可信度字段必须有中文标签，否则以英文原名直出。

  结论成立 / 裁判自证 / 出题侧自证三项是 property，
  不在 dataclass 的 __dict__ 里，静态扫描报不出来——只能在这里锁。
  """
  labels = _src("lib/labels.ts")
  for k in ("claim_valid", "self_certified", "exam_self_authored",
       "trust_note"):
    assert re.search(rf"^\s*{k}:", labels, re.M), (
      f"标签表缺少 {k} 的中文翻译")


def test_real_distill_produces_non_empty_trust_note():
  """真实跑一次蒸馏，结论是否成立 为假时 可信度说明 必须非空。

  这条守的是**端到端**：后端算出了原因，界面才有得显示。
  若 可信度说明 为空，ArenaPage 会退回硬编码文案——正是本次改动修掉的
  误导行为，而这个回退在类型检查层面看不出任何问题。

  在独立临时目录里跑，避免污染真实 home（本项目已多次栽在
  测试全局状态污染上）。
  """
  home = tempfile.mkdtemp(prefix="trust_note_")
  src = os.path.join(home, "src.md")
  Path(src).write_text("# 源\n\n一个做代码审查的 Agent。\n", encoding="utf-8")
  out = os.path.join(home, "out")
  os.makedirs(out, exist_ok=True)
  code = (
    "import os, sys, json\n"
    f"sys.path.insert(0, {str(ROOT)!r})\n"
    "os.environ['MOCK'] = '1'\n"
    f"os.environ['OMEGAFORGE_HOME'] = {home!r}\n"
    "from omegaforge.llm.client import LLMClient\n"
    "from omegaforge.core.budget import TokenBank\n"
    "from omegaforge.distill.engine import DistillEngine\n"
    "eng = DistillEngine(LLMClient(), TokenBank(budget_tokens=200000),\n"
    "          arena_rounds=1, max_generations=1, verbose=False)\n"
    f"g, rep = eng.distill({src!r}, output_dir={out!r})\n"
    "d = rep.to_dict()\n"
    "print(json.dumps({'claim_valid': d.get('claim_valid'),\n"
    "         'self_certified': d.get('self_certified'),\n"
    "         'trust_note': d.get('trust_note', '')},\n"
    "         ensure_ascii=False))\n"
  )
  r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
            capture_output=True, text=True, timeout=600)
  assert r.returncode == 0, f"蒸馏未跑通：{r.stderr[-800:]}"
  payload = json.loads(r.stdout.strip().splitlines()[-1])
  if payload["claim_valid"] is False:
    assert payload["trust_note"].strip(), (
      "claim_valid 为假但 trust_note 为空——界面会退回硬编码的 "
      "naive 文案，在自证/考题自出场景下给出错误原因。"
      f"状态：self_certified={payload['self_certified']}")
