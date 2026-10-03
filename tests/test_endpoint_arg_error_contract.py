"""入参异常文案守卫：真起服务，逐条发畸形入参，核对外层文案。

为什么必须真发请求
------------------
字段名与类型错误在静态检查里看不出来：`api.ts` 里这些调用的返回类型多为
宽泛的对象类型，写错字段名不报类型错误；构建与静态契约只看路径不看
入参。唯一能暴露的方式就是真发一次请求。

两条判据
--------
1. 不能落到「请求内容有误，请检查后重试」这类无法行动的兜底 —— 用户
  照着它只会盲目重试，而重试永远不会成功。
2. 不能把内部名（异常类名、字段名、本机路径）带进用户可见文案。

已审校的业务校验必须抛 UserError：抛 ValueError 会被统一压成兜底文案，
写好的中文提示根本到不了界面（权限级别、对话编号两处均为此形态）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = "/data/workspace/LATEST"
sys.path.insert(0, ROOT)

PORT = 8803
BASE = f"http://127.0.0.1:{PORT}"

GENERIC = ("操作失败，请稍后重试", "服务器故障", "内部错误",
      "请求内容有误，请检查后重试")
INTERNAL_HINTS = ("TaskStore", "UsageStore", "traceback", ".omegaforge",
         "Exception", "Error:", "/data/", "/home/", "/tmp/")

# (路径, 入参, 文案里必须出现的可行动信息)
CASES = [
  ("/api/distill", {}, "源 Agent"),
  ("/api/chat", {}, "消息"),
  ("/api/conversations/delete", {}, "对话"),
  ("/api/conversations/delete", {"id": 123}, "对话"),
  ("/api/conversations/model", {}, "对话"),
  ("/api/conversations/model", {"id": "x", "model": 123}, "对话"),
  ("/api/voice/tts", {}, "朗读"),
  # 数字入参按设计会收敛成文本（与表单行为一致），因此这条的返回取决于
  # 语音组件是否已安装：未安装时给出安装引导，同样可行动。不放进
  # 「必须点明字段」的清单，避免把环境差异误判成文案缺陷。
  ("/api/voice/tts", {"text": "hi", "speed": "fast"}, "语速"),
  ("/api/tools/policy", {"mode": "weird"}, "权限级别"),
  ("/api/tools/policy", {"mode": 123}, "权限级别"),
  ("/api/tools/exec", {}, "工具"),
  ("/api/kb/add", {}, "标题"),
  ("/api/wiki/save", {}, "标识"),
  ("/api/tasks/add", {}, "任务"),
  ("/api/tasks/add", {"text": "t", "priority": "high"}, "优先级"),
  ("/api/tasks/done", {}, "任务编号"),
  ("/api/skills/install", {}, "技能包"),
  ("/api/skills/invoke", {}, "技能名称"),
  ("/api/memory/remember", {}, "记住"),
  ("/api/providers/apply", {}, "供应商"),
  ("/api/providers/apply", {"name": "openai", "models": "x"}, "模型配置"),
  ("/api/providers/test", {}, "接口地址"),
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
  home = str(tmp_path_factory.mktemp("arg_home"))
  os.environ["OMEGAFORGE_HOME"] = home
  os.environ["OMEGAFORGE_MODE"] = "auto_edit"
  import omegaforge.server as srv
  srv.RUNS.home = None
  httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
  t = threading.Thread(target=httpd.serve_forever, daemon=True)
  t.start()
  yield srv
  httpd.shutdown()


def _post(path: str, payload) -> tuple[int, str]:
  body = json.dumps(payload).encode()
  req = urllib.request.Request(BASE + path, data=body,
                 headers={"Content-Type": "application/json"})
  try:
    with urllib.request.urlopen(req, timeout=30) as r:
      return r.status, r.read().decode("utf-8", "replace")
  except urllib.error.HTTPError as e:
    return e.code, e.read().decode("utf-8", "replace")


def _message(raw: str) -> str:
  try:
    d = json.loads(raw)
    return str(d.get("error") or d.get("message") or "")
  except Exception:
    return raw[:200]


@pytest.mark.parametrize("path,payload", [(c[0], c[1]) for c in CASES])
def test_no_generic_fallback(server, path, payload):
  code, raw = _post(path, payload)
  msg = _message(raw)
  assert code == 400, f"{path} 应拒绝，实际 {code}：{msg}"
  assert not any(g in msg for g in GENERIC), \
    f"{path} 落到无法行动的兜底：{msg}"
  assert msg.strip(), f"{path} 返回了空文案"


@pytest.mark.parametrize("path,payload,expect", CASES)
def test_message_points_at_the_field(server, path, payload, expect):
  code, raw = _post(path, payload)
  msg = _message(raw)
  assert expect in msg, f"{path} 文案未点明「{expect}」：{msg}"


@pytest.mark.parametrize("path,payload", [(c[0], c[1]) for c in CASES])
def test_message_has_no_internal_names(server, path, payload):
  _, raw = _post(path, payload)
  msg = _message(raw)
  for h in INTERNAL_HINTS:
    assert h not in msg, f"{path} 文案带内部名「{h}」：{msg}"


def test_policy_mode_lists_all_valid_levels(server):
  """权限级别非法时必须列出全部合法级别，用户才知道能选什么。"""
  _, raw = _post("/api/tools/policy", {"mode": "weird"})
  msg = _message(raw)
  for label in ("变更前确认", "自动编辑", "计划模式", "完全访问"):
    assert label in msg, f"未列出级别「{label}」：{msg}"


def test_numeric_text_argument_is_normalized_not_rejected(server):
  """数字入参按设计收敛为文本，不是缺陷：与表单提交的行为一致。

  列表与对象这类无法无损转成文字的结构才拒绝。这条单独列出，是为了让
  "这里没有报错"不被人误读成漏检。
  """
  from omegaforge.core.validate import as_text
  assert as_text({"x": 123}, "x", label="正文") == "123"
  for bad in (["a"], {"a": 1}):
    with pytest.raises(Exception) as e:
      as_text({"x": bad}, "x", label="正文")
    assert "文字" in str(e.value)


def test_missing_argument_says_what_to_fill_not_malformed(server):
  """缺项必须说"缺什么"，不能落到"格式不正确"。

  这两句都含"对话"，只断言包含关键词会漏掉区别：格式校验的文案同样能
  满足"点明字段"的判据。用户看到"格式不正确"会以为自己填错了，而实际
  是根本没填——界面上点删除属于后者，重试永远不会成功。
  """
  for path in ("/api/conversations/delete", "/api/conversations/model"):
    _, raw = _post(path, {})
    msg = _message(raw)
    assert "请填写" in msg, f"{path} 缺项未提示填写内容：{msg}"
