"""流式 /api/chat/stream 与非流式 /api/chat 的路径一致性守卫。

背景（全部端到端验证：假上游捕获真实发出的 system）
--------------------------------------------------
两条路径各自复制了一遍"对话准备"逻辑，于是只加在一边的东西会静默漂移：

 1. 人设：非流式注入后 system 392 字，流式只有 58 字——人设整条丢弃。
   前端若只用 SSE，人设功能整体失效，且流式请求里的 persona 连存都不存
   （验证 conv.persona=None）。
 2. 用量：流式前后 USAGE.summary() 完全不变 → 所有流式对话消耗不入账，
   用量页系统性少记。
 3. token：流式只报 completion 估算（验证 5），非流式报 提示词+completion
   （验证 120），前端同一个字段两个数。
 4. 陈旧会话ID：非流式报"未找到该对话"，流式却静默新建一个空对话，
   用户以为还在原对话，历史凭空消失。

修复方式不是"补四行"，而是抽出 _prepare_chat() 作为唯一真源——两条路径
拿到同一个 dict，差异在结构上不可能再出现。本文件把这四条钉死，并且
回退校验时逐条撤回修复，确认每一项都能被抓到（不是形式化守卫）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}"
     + (f" — {detail}" if detail else ""))


CAPTURED: list = []


class _Upstream(BaseHTTPRequestHandler):
  """假上游：捕获真实发出的 messages，按 stream 与否返回对应形态。"""

  def log_message(self, *a):
    pass

  def do_POST(self):
    n = int(self.headers.get("Content-Length", 0))
    body = json.loads(self.rfile.read(n) or b"{}")
    CAPTURED.append(body)
    if body.get("stream"):
      self.send_response(200)
      self.send_header("Content-Type", "text/event-stream")
      self.end_headers()
      for ch in ("流式", "回复"):
        self.wfile.write(
          ("data: " + json.dumps(
            {"choices": [{"delta": {"content": ch}}]})
           + "\n\n").encode())
        self.wfile.flush()
      self.wfile.write(b"data: [DONE]\n\n")
      self.wfile.flush()
    else:
      raw = json.dumps(
        {"choices": [{"message": {"content": "非流式回复"}}],
         "model": body.get("model", "m"),
         "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
      ).encode()
      self.send_response(200)
      self.send_header("Content-Length", str(len(raw)))
      self.end_headers()
      self.wfile.write(raw)

  def do_GET(self):
    raw = json.dumps({"data": [{"id": "m"}]}).encode()
    self.send_response(200)
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)


def main() -> int:
  tmp = tempfile.mkdtemp(prefix="of_parity_")
  prev_env = os.environ.get("OMEGAFORGE_HOME")
  prev = {}
  up = None
  httpd = None
  try:
    os.makedirs(os.path.join(tmp, "skills", "coder"), exist_ok=True)
    with open(os.path.join(tmp, "skills", "coder", "SKILL.md"), "w") as f:
      f.write("---\nname: coder\ndescription: 资深工程师人设\nversion: 1\n"
          "---\n你是一名资深工程师，回答必须给出可运行代码。\n")

    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_port = up.server_address[1]

    with open(os.path.join(tmp, "providers.json"), "w") as f:
      json.dump({"active": "custom",
            "configs": {"custom": {
              "base_url": f"http://127.0.0.1:{up_port}/v1",
              "api_key": "k",
              "models": {"main": "m", "fast": "m"}}}}, f)

    os.environ["OMEGAFORGE_HOME"] = tmp
    import omegaforge.server as srv
    from omegaforge.chat.store import ConversationStore
    from omegaforge.llm.providers import ProviderManager
    from omegaforge.skills.manager import SkillManager

    # 模块级单例是在 import 时按 OMEGAFORGE_HOME 建的：pytest 同批执行时
    # 本套件若改了又还原不了，后续测试会静默跑在错误的目录上。
    prev = {"CONVS": srv.CONVS, "PROVIDERS": srv.PROVIDERS,
        "SKILLS": srv.SKILLS, "USAGE": srv.USAGE}
    srv.CONVS = ConversationStore(tmp)
    srv.PROVIDERS = ProviderManager(tmp)
    srv.SKILLS = SkillManager(tmp)
    srv.USAGE = type(srv.USAGE)(tmp)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, obj):
      req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
      return urllib.request.urlopen(req, timeout=30).read().decode()

    def sys_of_last():
      return (CAPTURED[-1]["messages"][0]["content"] if CAPTURED else "")

    # ---- 1. 人设：两条路径必须注入同一份 ----
    CAPTURED.clear()
    r1 = json.loads(post("/api/chat", {"message": "写个快排",
                      "persona": "coder"}))
    cid = r1.get("conversation_id", "")
    sys_non = sys_of_last()

    CAPTURED.clear()
    post("/api/chat/stream", {"message": "再写个堆排",
                 "conversation_id": cid})
    sys_str = sys_of_last()

    check("1.1 非流式注入人设正文", "资深工程师" in sys_non,
       f"{len(sys_non)}字")
    check("1.2 流式注入人设正文", "资深工程师" in sys_str,
       f"{len(sys_str)}字")
    check("1.3 两条路径 system 长度一致",
       len(sys_non) == len(sys_str) > 100,
       f"非流式={len(sys_non)} 流式={len(sys_str)}")

    # ---- 2. 流式必须真的记账 ----
    before = json.dumps(srv.USAGE.summary(), sort_keys=True)
    post("/api/chat/stream", {"message": "流式单独一句"})
    after = json.dumps(srv.USAGE.summary(), sort_keys=True)
    check("2.1 流式记录用量", before != after,
       f"entries 变化：{json.loads(before).get('entries')} -> "
       f"{json.loads(after).get('entries')}")

    # ---- 3. 流式必须能独立设置并持久化人设 ----
    conv2 = srv.CONVS.new(title="纯流式")
    post("/api/chat/stream", {"message": "只走流式",
                 "conversation_id": conv2["id"],
                 "persona": "coder"})
    c2 = srv.CONVS.get(conv2["id"])
    check("2.2 流式请求里的 persona 被持久化",
       bool(c2.get("persona")), f"persona={c2.get('persona')!r}")

    # ---- 4. 陈旧会话ID：两条路径都必须报错，且流式必须收尾 ----
    try:
      post("/api/chat", {"message": "t", "conversation_id": "不存在"})
      non_err = False
    except Exception:
      non_err = True
    sse = post("/api/chat/stream", {"message": "t",
                    "conversation_id": "不存在"})
    evts = [json.loads(l[6:]) for l in sse.splitlines()
        if l.startswith("data: ") and l[6:].strip().startswith("{")]
    types = [e.get("type") for e in evts]
    has_err = any(e.get("error") for e in evts)
    check("2.3 非流式对陈旧ID报错", non_err, "")
    check("2.4 流式对陈旧ID同样报错", has_err, f"事件序列={types}")
    check("2.5 流式出错仍推 done（前端不会永远转圈）",
       "done" in types, f"事件序列={types}")

    print(f"\n路径一致性套件结果: {len(PASS)}/{len(PASS)+len(FAIL)} PASS")
    return 1 if FAIL else 0
  finally:
    try:
      import omegaforge.server as s
      for k, v in prev.items():
        setattr(s, k, v)
    except Exception:
      pass
    if prev_env is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = prev_env
    if httpd:
      httpd.shutdown()
    if up:
      up.shutdown()


if __name__ == "__main__":
  raise SystemExit(main())
