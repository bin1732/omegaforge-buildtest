"""对话层 + 记忆层契约守卫。

每一条都对应一次可复现的故障（不是读代码推演），并且都做过回退校验：
把对应修复撤掉，这里的断言必须变红——否则它就是形式化。

回退校验记录见 docs/权限四级与门禁体系.md 。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.chat.store import ConversationStore     # noqa: E402
from omegaforge.core.errors import UserError         # noqa: E402
from omegaforge.memory.kb import KnowledgeBase        # noqa: E402
from omegaforge.memory.tasks import Tasks          # noqa: E402
from omegaforge.memory.wiki import Wiki           # noqa: E402


@pytest.fixture()
def home(tmp_path):
  return str(tmp_path)


# ---------------------------------------------------------------
# 1. build_context 必须携带历史（缺少该约束时整段丢弃）
# ---------------------------------------------------------------
def test_build_context_carries_history(home):
  cs = ConversationStore(home)
  c = cs.new(title="历史测试")
  cs.add_message(c["id"], "user", "第一轮问题")
  cs.add_message(c["id"], "assistant", "第一轮回答")
  cs.add_message(c["id"], "user", "第二轮问题")
  conv = cs.get(c["id"])
  system, payload = cs.build_context(conv, "第三轮问题")
  assert "第一轮问题" in payload, "历史原文必须进入上下文"
  assert "第一轮回答" in payload
  assert "第三轮问题" in payload


def test_build_context_no_duplicate_current_turn(home):
  """server.py 先 append 当前发言再调 build_context：不能出现两遍。"""
  cs = ConversationStore(home)
  c = cs.new(title="去重")
  conv = cs.add_message(c["id"], "user", "当前这句话")
  _, payload = cs.build_context(conv, "当前这句话")
  assert payload.count("当前这句话") == 1, "当前发言不得重复出现"


def test_build_context_dirty_messages_survive(home):
  """messages 里混进 None / 非 dict / content=null：不得崩、不得泄漏 None。"""
  cs = ConversationStore(home)
  conv = {"id": "aaaaaaaaaa", "summary": None,
      "messages": [None, 123, {"role": "user", "content": None},
             {"role": "user", "content": "正常一句"}]}
  system, payload = cs.build_context(conv, "任务")
  assert "正常一句" in payload
  assert "None" not in payload, "脏消息的 None 不得渲染进上下文"


# ---------------------------------------------------------------
# 2. 压缩阈值必须是 token 口径（中文不再被推迟 4 倍）
# ---------------------------------------------------------------
def test_cjk_long_conversation_compacts(home):
  """14000 汉字 ≈ 14k tokens，远超 6k：必须触发压缩。"""
  cs = ConversationStore(home)
  c = cs.new(title="长中文对话")
  for _ in range(20):
    cs.add_message(c["id"], "user", "中" * 700)
  conv = cs.get(c["id"])
  assert conv.get("compacted") is True, "中文长对话必须触发压缩"
  assert len(conv["messages"]) < 20


def test_english_threshold_unchanged(home):
  """24000 英文字符 ≈ 6k tokens：刚好触发（与缺少该约束时一致，防处理过头）。"""
  cs = ConversationStore(home)
  c = cs.new(title="长英文对话")
  for _ in range(20):
    cs.add_message(c["id"], "user", "a" * 1200)
  conv = cs.get(c["id"])
  assert conv.get("compacted") is True


# ---------------------------------------------------------------
# 3. tasks：脏记录不得让任务页 500
# ---------------------------------------------------------------
def _mk_tasks(tmp_path, obj):
  h = os.path.join(str(tmp_path), "t" + os.urandom(3).hex())
  os.makedirs(h)
  with open(os.path.join(h, "tasks.json"), "w", encoding="utf-8") as f:
    json.dump(obj, f)
  return Tasks(h)


def test_tasks_dirty_priority_and_missing_done(tmp_path):
  t = _mk_tasks(tmp_path, {
    "a": {"id": "a", "text": "优先级越界", "done": False,
       "priority": 9, "created": 1},
    "b": {"id": "b", "text": "优先级是字符串", "done": False,
       "priority": "2", "created": 2},
    "c": {"id": "c", "text": "缺 done 字段", "priority": 1,
       "created": 3},
    "d": {"id": "d", "text": "created 是字符串", "done": False,
       "priority": 1, "created": "昨天"},
    "e": "我根本不是字典",
  })
  items = t.list()
  assert len(items) == 4, "脏 priority/done/created 不得崩，非字典条目剔除"
  assert t.stats()["pending"] == 4


def test_tasks_add_dirty_priority(tmp_path):
  h = os.path.join(str(tmp_path), "tadd")
  os.makedirs(h)
  t = Tasks(h)
  for bad in ("high", None, [1], {"a": 1}, 1.5, 99):
    it = t.add("任务", bad)
    assert it["priority"] in (1, 2, 3), f"priority={bad!r} 必须收敛"


def test_tasks_complete_missing_done(tmp_path):
  t = _mk_tasks(tmp_path, {"a": {"id": "a", "text": "x", "priority": 1}})
  assert t.complete("a") is True, "缺 done 字段应视为未完成并可完成"
  assert t.reopen("a") is True


# ---------------------------------------------------------------
# 4. kb：脏文档不得让检索 500
# ---------------------------------------------------------------
def _mk_kb(tmp_path, obj):
  h = os.path.join(str(tmp_path), "k" + os.urandom(3).hex())
  os.makedirs(h)
  with open(os.path.join(h, "kb.json"), "w", encoding="utf-8") as f:
    json.dump(obj, f)
  return KnowledgeBase(h)


def test_kb_non_dict_value_skipped(tmp_path):
  kb = _mk_kb(tmp_path, {"a": "我不是字典",
              "b": {"id": "b", "type": "note",
                 "title": "正常", "text": "正文", "ts": 1}})
  assert kb.search("正文") != []
  assert kb.count() == 1, "非字典条目必须剔除"


def test_kb_dirty_ts_and_null_fields(tmp_path):
  kb = _mk_kb(tmp_path, {"a": {"id": "a", "type": "note", "title": None,
                 "text": None, "tags": None, "ts": "昨天"}})
  assert kb.all() is not None
  assert kb.search("测试") == []


def test_kb_oversized_doc_rejected_with_chinese(tmp_path):
  h = os.path.join(str(tmp_path), "kbig")
  os.makedirs(h)
  kb = KnowledgeBase(h)
  with pytest.raises(UserError) as e:
    kb.add("大文档", "x" * 300_000)
  assert "内容过长" in str(e.value), "超限必须给可自助处理的中文提示"


def test_kb_search_dirty_limit(tmp_path):
  kb = _mk_kb(tmp_path, {"a": {"id": "a", "type": "note",
                 "title": "标题", "text": "内容", "ts": 1}})
  for bad in ("5", None, [1], 0, -3):
    hits = kb.search("标题", limit=bad)
    # 只调用不看结果等于没测：把脏上限当成"返回空"同样不报错，
    # 界面上表现为"搜什么都没有"，而库里明明存着。
    assert any(h.get("id") == "a" for h in hits), (
      f"上限参数不合法时应回退默认值并照常命中，limit={bad!r} 时未命中")


# ---------------------------------------------------------------
# 5. wiki：标题不得污染 frontmatter
# ---------------------------------------------------------------
def test_wiki_title_newline_cannot_inject_body(tmp_path):
  w = Wiki(str(tmp_path))
  w.save("page", "标题\nupdated: 0\n\n伪造正文", "真实正文")
  pg = w.get("page")
  assert "\n" not in pg["title"], "标题必须压成单行"
  assert pg["body"].strip() == "真实正文", "正文不得被标题注入顶替"


def test_wiki_title_none_not_literal_none(tmp_path):
  w = Wiki(str(tmp_path))
  w.save("p2", None, "正文")
  assert w.get("p2")["title"] == "p2", "空标题回落 slug，不得存成 'None'"


def test_wiki_non_utf8_page_does_not_break_listing(tmp_path):
  d = os.path.join(str(tmp_path), "wiki")
  os.makedirs(d)
  with open(os.path.join(d, "bad.md"), "wb") as f:
    f.write(b"title: \xff\xfe binary\n\nbody")
  with open(os.path.join(d, "good.md"), "w", encoding="utf-8") as f:
    f.write("title: 正常\nupdated: 1\n\n正文")
  w = Wiki(str(tmp_path))
  slugs = [p["slug"] for p in w.pages()]
  assert "good" in slugs, "一个非 UTF-8 文件不得让整个词条列表 500"
