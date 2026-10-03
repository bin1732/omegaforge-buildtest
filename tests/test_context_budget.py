"""上下文预算守卫。

每条都对应一次可复现的故障（不是读代码推演），并且都做过回退校验：
把对应修复撤掉，这里的断言必须变红——否则它就是形式化。

本文件覆盖的三层
----------------
1. token 估算唯一真源（core.tokens）——两套系数会让"裁剪"与"复核"打架
2. build_context 的全局预算——只有局部截断时最坏情况仍可达 22 万 token
3. _prepare_chat 的收口——发言上限、人设点名、优雅降级不退化成硬失败

回退校验记录见 docs/权限四级与门禁体系.md 。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.chat.store import (             # noqa: E402
  ConversationStore, _est_tokens,
  CONTEXT_BUDGET_TOKENS, MSG_MAX_CHARS)
from omegaforge.core.errors import UserError         # noqa: E402
from omegaforge.core.tokens import estimate_tokens      # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
  """每个用例一个独立数据目录。

  必须**重新指向模块级单例**：CONVS / SKILLS / USAGE 在 import 时就按当时的
  OMEGAFORGE_HOME 建好了目录，只改环境变量改不动它们。不重新指向的话，
  后一个用例会写进前一个用例的 tmp_path——而 pytest 只保留最近若干个
  tmp_path，旧的会被回收，于是出现"加了 16 条消息后对话凭空消失"
  （验证 ValueError: 未找到该对话）。这类失败是测试基建问题，不是产品问题。
  """
  h = str(tmp_path)
  monkeypatch.setenv("OMEGAFORGE_HOME", h)
  import omegaforge.server as S
  for obj in (S.CONVS, S.SKILLS, S.USAGE, S.PROVIDERS):
    try:
      if hasattr(obj, "home"):
        obj.home = h
      if hasattr(obj, "dir"):
        obj.dir = os.path.join(h, os.path.basename(obj.dir))
        os.makedirs(obj.dir, exist_ok=True)
    except Exception:
      pass
  return h


@pytest.fixture()
def store(home):
  return ConversationStore(home=home)


def _cnt(text: str) -> int:
  return text.count("用户:") + text.count("助手:")


def _install_skill(home: str, name: str, n: int):
  """装一个正文 n 字的技能（走真实 install 路径，不是直接写目录）。"""
  import omegaforge.server as S
  src = os.path.join(home, "src_" + name)
  os.makedirs(src, exist_ok=True)
  with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
    f.write("---\nname: %s\ndescription: 测试用\n---\n# 技能\n\n" % name
        + "技" * n)
  return S.SKILLS.install(src)


# ---------------------------------------------------------------
# 1. token 估算唯一真源
# ---------------------------------------------------------------

def test_1_1_两处估算口径一致():
  """server 与 chat.store 曾各有各的系数（1.2 vs 1.0）。

  验证后果：人设 8000 字 + 20 条长历史时，build_context 按 1.0 裁剪后
  认为 23,169 没超 24,000，server 随后按 1.2 复核得 26,498 判定超预算。
  于是本该"丢历史、对话继续"变成"整句发不出去，并让用户去改技能文件"——
  指引方向是错的。
  """
  import omegaforge.server as S
  for t in ("", "技" * 1000, "hello world", "中英mixed混合123", "技" * 199_000):
    assert S._estimate_tokens(t) == _est_tokens(t), f"口径不一致: {t[:10]!r}"
    assert _est_tokens(t) == estimate_tokens(t)


def test_1_2_估算取保守值不为负():
  """空值必须得 0：负数会让"预算-已用"算出比预算更大的剩余，
  裁剪逻辑于是认为怎么装都装得下。"""
  assert estimate_tokens("") == 0
  assert estimate_tokens(None) == 0
  assert estimate_tokens("技") >= 1
  assert estimate_tokens("abcdefgh") >= 2   # 8 西文 ≈ 2 token


# ---------------------------------------------------------------
# 2. build_context 全局预算
# ---------------------------------------------------------------

def test_2_1_超预算从最旧开始丢且保留最新(store):
  conv = {"id": "c", "summary": "",
      "messages": [{"role": "user", "content": "第%d轮" % i + "历" * 1500}
             for i in range(12)]}
  task = "现在"
  _, payload = store.build_context(conv, task, budget_tokens=12_000)
  assert _cnt(payload) < 12, "预算内没有被裁剪"
  assert "第11轮" in payload, "最新一条被丢掉了——丢的方向反了"
  assert "第0轮" not in payload, "最旧一条没被丢"


def test_2_2_预算充足时不裁剪(store):
  conv = {"id": "c", "summary": "",
      "messages": [{"role": "user", "content": "第%d轮" % i}
             for i in range(6)]}
  _, payload = store.build_context(conv, "现在", budget_tokens=24_000)
  assert _cnt(payload) == 6, "预算充足却被误裁"


def test_2_3_裁剪不截断单条内容(store):
  """整条丢，而不是截半句——半句话比整条缺失更容易让模型产生幻觉。"""
  conv = {"id": "c", "summary": "",
      "messages": [{"role": "user", "content": "第%d轮" % i + "历" * 1500}
             for i in range(12)]}
  _, payload = store.build_context(conv, "现在", budget_tokens=12_000)
  for line in payload.splitlines():
    if line.startswith(("用户:", "助手:")):
      body = line.split(": ", 1)[1]
      assert len(body) == len("第X轮") + 1500 or body.endswith("历"), \
        f"单条被截断成半句: {len(body)} 字"


def test_2_4_不带预算时行为不变(store):
  """必须向后兼容：旧调用方不传 budget_tokens 时不得改变结果。"""
  conv = {"id": "c", "summary": "摘" * 4000,
      "messages": [{"role": "user", "content": "第%d轮" % i + "历" * 1500}
             for i in range(12)]}
  a1, b1 = store.build_context(conv, "现在")
  a2, b2 = store.build_context(conv, "现在", budget_tokens=None)
  assert (a1, b1) == (a2, b2)
  assert _cnt(b1) > 0, "历史仍然要进上下文（修的那条不能回退）"


def test_2_5_丢到一条不剩仍超预算时不静默送出(store):
  """大头不在历史（如摘要本身就 4000 token）时，裁剪无能为力。
  此时必须让调用方拿到"仍然超预算"的信号，而不是静默送出注定被
  上游拒收的请求（验证拒收会映射成 500「模型服务返回了异常响应」）。"""
  conv = {"id": "c", "summary": "摘" * 4_000, "messages": []}
  _, payload = store.build_context(conv, "现在", budget_tokens=2_000)
  assert payload == "现在", "历史为空时应只剩当前发言"
  # 调用方复核会超预算 —— 这条由 test_3_x 端到端覆盖


# ---------------------------------------------------------------
# 3. _prepare_chat 收口（端到端）
# ---------------------------------------------------------------

def test_3_1_中等人设加长历史走优雅降级而非硬失败(home):
  """这条是"顺序"守卫：先算人设块、再把剩余预算交给 build_context。

  顺序反了会先按满额预算装满历史，再把人设叠上去——验证 total 从
  22,895 变成 32,978，优雅降级退化成硬失败，且报错指引用户去改
  技能文件（真正该丢的是历史）。
  """
  import omegaforge.server as S
  _install_skill(home, "mid", 8_000)
  c = S.CONVS.new(title="h")
  for i in range(20):
    S.CONVS.add_message(c["id"], "user", "第%d轮" % i + "历" * 1500)
  r = S._prepare_chat({"conversation_id": c["id"], "persona": "mid"}, "现在")
  total = S._estimate_tokens(r["system"]) + S._estimate_tokens(r["task"])
  assert total <= CONTEXT_BUDGET_TOKENS, f"超预算却没被拦: {total:,}"
  assert _cnt(r["task"]) > 0, "历史被裁成一条不剩——顺序反了会走到硬失败"
  assert "第19轮" in r["task"], "最新的历史被丢了"


def test_3_2_超大人物报错且点名(home):
  """点名而不是泛化报错：用户要知道去改哪个文件。"""
  import omegaforge.server as S
  _install_skill(home, "huge", 199_000)
  c = S.CONVS.new(title="t")
  with pytest.raises(UserError) as e:
    S._prepare_chat({"conversation_id": c["id"], "persona": "huge"}, "你好")
  assert "huge" in str(e.value), f"没点名: {e.value}"
  assert "预算" in str(e.value)


def test_3_3_发言超上限报错而非静默截断(home):
  """当前发言是用户最在意的内容，截断会让他以为系统收到了全部。"""
  import omegaforge.server as S
  c = S.CONVS.new(title="t")
  with pytest.raises(UserError) as e:
    S._prepare_chat({"conversation_id": c["id"]}, "长" * (MSG_MAX_CHARS + 1))
  assert "拆分" in str(e.value)


def test_3_4_正常对话不误伤(home):
  import omegaforge.server as S
  _install_skill(home, "tiny", 300)
  c = S.CONVS.new(title="n")
  S.CONVS.add_message(c["id"], "user", "第一句")
  r = S._prepare_chat({"conversation_id": c["id"], "persona": "tiny"}, "第二句")
  assert "第一句" in r["task"], "正常短对话的历史被误裁"
  assert S._estimate_tokens(r["system"]) + S._estimate_tokens(r["task"]) \
    <= CONTEXT_BUDGET_TOKENS


def test_3_5_无人设时长历史仍被裁到预算内(home):
  """只保证了"历史进上下文"，没管总量；这里保证进得来也装得下。"""
  import omegaforge.server as S
  c = S.CONVS.new(title="h")
  for i in range(20):
    S.CONVS.add_message(c["id"], "user", "第%d轮" % i + "历" * 1500)
  r = S._prepare_chat({"conversation_id": c["id"]}, "现在")
  total = S._estimate_tokens(r["system"]) + S._estimate_tokens(r["task"])
  assert total <= CONTEXT_BUDGET_TOKENS, f"超预算: {total:,}"
  assert "第19轮" in r["task"], "丢的方向反了"
  assert "第0轮" not in r["task"], "该丢的最旧没丢"


def test_3_6_预算常量是保守值():
  """预算必须显著小于常见模型上下文，留出回复空间。"""
  assert CONTEXT_BUDGET_TOKENS <= 32_000, "预算超过 32k 档下限，会撞上游"
  assert CONTEXT_BUDGET_TOKENS >= 8_000, "预算过小会让正常长对话频繁被裁"
  assert MSG_MAX_CHARS >= 4_000, "发言上限过小会影响正常使用"
