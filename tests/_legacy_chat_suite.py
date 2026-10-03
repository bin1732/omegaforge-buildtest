"""OmegaForge 对话系统端到端测试：持久化 / 超长压缩 / auto 路由 / API 全链路。

运行: python3 tests/_legacy_chat_suite.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="of_chat_")
os.environ["OMEGAFORGE_HOME"] = TMP

RESULTS = []


def check(name, fn):
  try:
    fn()
    RESULTS.append(True)
    print(f" ✅ {name}")
  except Exception as e:           # noqa: BLE001
    RESULTS.append(False)
    print(f" ❌ {name} → {type(e).__name__}: {str(e)[:140]}")


from omegaforge.chat.store import ConversationStore, AutoRouter # noqa: E402

print("◆ 17. 对话存储与持久化")
cs = ConversationStore(TMP)
c = cs.new("测试对话")
check("17.1 新建会话", lambda: (_ for _ in ()).throw(AssertionError())
   if not (c["id"] and c["model_pref"] == "auto") else None)
cs.add_message(c["id"], "user", "你好")
cs.add_message(c["id"], "assistant", "你好，我是蒸馏助手", model="mock")
c2 = ConversationStore(TMP).get(c["id"])
check("17.2 持久化（跨实例重开）", lambda: (_ for _ in ()).throw(AssertionError())
   if len(c2["messages"]) != 2 else None)
check("17.3 消息含模型署名", lambda: (_ for _ in ()).throw(AssertionError())
   if c2["messages"][1].get("model") != "mock" else None)
check("17.4 会话列表", lambda: (_ for _ in ()).throw(AssertionError())
   if [x["id"] for x in cs.list()] != [c["id"]] else None)
cs.set_model_pref(c["id"], "glm-4.5-flash")
check("17.5 会话级模型偏好", lambda: (_ for _ in ()).throw(AssertionError())
   if cs.get(c["id"])["model_pref"] != "glm-4.5-flash" else None)
check("17.6 删除", lambda: (_ for _ in ()).throw(AssertionError())
   if not cs.delete(c["id"]) or cs.get(c["id"]) else None)

print("◆ 18. 超长对话压缩")
big = cs.new("长对话")
for i in range(40):
  cs.add_message(big["id"], "user", f"问题 {i}：" + "细节" * 300)
  cs.add_message(big["id"], "assistant", f"回答 {i}：" + "分析" * 300)
b = cs.get(big["id"])
check("18.1 旧消息被压缩为摘要", lambda: (_ for _ in ()).throw(AssertionError())
   if len(b["messages"]) >= 40 or not b.get("summary") else None)
check("18.2 最近原文保留", lambda: (_ for _ in ()).throw(AssertionError())
   if "回答 39" not in b["messages"][-1]["content"] else None)
check("18.3 摘要有界（≤4000 字符）", lambda: (_ for _ in ()).throw(AssertionError())
   if len(b["summary"]) > 4100 else None)

print("◆ 19. Auto 模型路由")
mf, mm = "fast-model", "main-model"
sel, why = AutoRouter.pick("帮我写一篇 2000 字的行业分析报告", mf, mm)
check("19.1 重任务→main", lambda: (_ for _ in ()).throw(AssertionError())
   if sel != mm else None)
sel, why = AutoRouter.pick("今天周几？", mf, mm)
check("19.2 轻任务→fast", lambda: (_ for _ in ()).throw(AssertionError())
   if sel != mf else None)
sel, _ = AutoRouter.pick("```python\ndef f(): pass\n```\n重构这段代码并实现单元测试", mf, mm)
check("19.3 代码任务→main", lambda: (_ for _ in ()).throw(AssertionError())
   if sel != mm else None)

print("◆ 20. Chat API 端到端（HTTP 全链路）")
from http.server import ThreadingHTTPServer         # noqa: E402
from omegaforge import server as srv            # noqa: E402
httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
# 固定端口在并发/残留进程下必然 Address already in use，改为系统分配
threading.Thread(target=httpd.serve_forever, daemon=True).start()
B = "http://127.0.0.1:%d" % httpd.server_address[1]


def post(p, d):
  req = urllib.request.Request(B + p, data=json.dumps(d).encode(),
                 headers={"Content-Type": "application/json"})
  return json.loads(urllib.request.urlopen(req, timeout=30).read())


def get(p):
  return json.loads(urllib.request.urlopen(B + p, timeout=10).read())


r = post("/api/chat", {"message": "用一句话介绍你自己"})
check("20.1 无 cid 自动建会话", lambda: (_ for _ in ()).throw(AssertionError())
   if not r.get("conversation_id") or not r.get("reply") else None)
cid = r["conversation_id"]
r = post("/api/chat", {"conversation_id": cid, "message": "继续"})
check("20.2 指定会话续聊", lambda: (_ for _ in ()).throw(AssertionError())
   if r["conversation_id"] != cid else None)
convs = get("/api/conversations")["conversations"]
check("20.3 会话出现在列表", lambda: (_ for _ in ()).throw(AssertionError())
   if cid not in [x["id"] for x in convs] else None)
full = get("/api/conversations/" + cid)
check("20.4 历史完整（2 轮=4 条）", lambda: (_ for _ in ()).throw(AssertionError())
   if len(full["messages"]) != 4 else None)
cat = get("/api/providers/models?name=zhipu")["catalog"]
check("20.5 国产模型目录（智谱 7 款）", lambda: (_ for _ in ()).throw(AssertionError())
   if len(cat) < 7 or "glm-4.5-flash" not in cat else None)
presets = get("/api/providers")["presets"]
names = [p["name"] for p in presets]
check("20.6 全国产服务商在册", lambda: (_ for _ in ()).throw(AssertionError())
   if not {"zhipu", "deepseek", "moonshot", "doubao", "qwen", "hunyuan",
       "qianfan", "minimax", "spark"} <= set(names) else None)
httpd.shutdown()

passed = sum(RESULTS)
print("\n" + "═" * 60)
print(f"对话套件结果: {passed}/{len(RESULTS)} PASS")
if passed != len(RESULTS):
  sys.exit(1)
print("对话系统全链路验证通过 ✅")
shutil.rmtree(TMP, ignore_errors=True)
