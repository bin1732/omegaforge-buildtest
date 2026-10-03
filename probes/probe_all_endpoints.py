#!/usr/bin/env python3
"""全端点联调探测：起真后端，逐条真发 HTTP，打印真实结果。

目的不是"证明没问题"，而是把 21 个 POST + 全部 GET 的真实状态摆出来，
再由结果决定哪些是真缺陷。不预设结论。
"""
import json, os, shutil, sys, threading, time, urllib.request, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"

HOME = "/tmp/probe_all_home"
shutil.rmtree(HOME, ignore_errors=True)
os.makedirs(HOME, exist_ok=True)
os.environ["OMEGAFORGE_HOME"] = HOME
os.environ["MOCK"] = "1"

import omegaforge.server as srv
from http.server import ThreadingHTTPServer
for nm in ("RUNS","CONVS","USAGE","TASKS","KB","WIKI","JOBS","MEMORY","PROVIDERS","SKILLS"):
    obj = getattr(srv, nm, None)
    if obj is not None and hasattr(obj, "home"):
        try: obj.home = HOME
        except Exception: pass
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.6)

def http(path, payload=None, timeout=60, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    m = method or ("POST" if data else "GET")
    req = urllib.request.Request(BASE+path, data=data, method=m,
        headers={"Content-Type":"application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try: return r.status, json.loads(raw)
            except Exception: return r.status, {"_raw": raw[:120]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8","replace")
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"_raw": raw[:120]}
    except Exception as e:
        return -1, {"_exc": f"{type(e).__name__}: {e}"}

# 准备真实素材：技能包 / wav
skill_dir = "/tmp/probe_skill"
shutil.rmtree(skill_dir, ignore_errors=True)
os.makedirs(skill_dir, exist_ok=True)
open(os.path.join(skill_dir,"SKILL.md"),"w",encoding="utf-8").write(
    "---\nname: probe-skill\ndescription: 联调用技能\ntype: tool\n---\n\n# 用法\n\n联调。\n")

# 最小 16bit PCM wav（0.01s 静音）
import base64, wave
wav_path = "/tmp/probe.wav"
with wave.open(wav_path,"wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00"*160)
audio_b64 = base64.b64encode(open(wav_path,"rb").read()).decode()

# 种子数据
http("/api/kb/add", {"title":"折扣策略","text":"满200减30","type":"note"})
http("/api/memory/remember", {"fact":"用户偏好中文回复"})
tid = http("/api/tasks/add", {"text":"联调待办"})[1].get("id")
cid = http("/api/conversations/new", {"title":"联调会话"})[1].get("id")

POSTS = [
    ("/api/distill", {"source_prompt":"你是助手","task":"联调"}),
    ("/api/chat", {"message":"你好","conversation_id":cid}),
    ("/api/conversations/new", {"title":"新建联调"}),
    ("/api/conversations/model", {"conversation_id":cid,"model":"auto"}),
    ("/api/conversations/delete", {"conversation_id":cid}),
    ("/api/memory/remember", {"fact":"联调事实"}),
    ("/api/memory/recall", {"q":"偏好"}),
    ("/api/kb/add", {"title":"联调条目","text":"内容"}),
    ("/api/kb/search", {"q":"折扣"}),
    ("/api/tasks/add", {"text":"联调任务","priority":2}),
    ("/api/tasks/done", {"task_id":tid}),
    ("/api/wiki/save", {"slug":"probe","title":"联调","body":"内容"}),
    ("/api/skills/install", {"path":skill_dir}),
    ("/api/skills/invoke", {"name":"probe-skill","context":"x"}),
    ("/api/tools/permissions", {"fs_read":True}),
    ("/api/tools/policy", {"mode":"confirm"}),
    ("/api/tools/exec", {"name":"fs_list","arguments":{"path":"."}}),
    ("/api/voice/asr", {"audio_b64":audio_b64}),
    ("/api/voice/tts", {"text":"你好","speed":1.0}),
    ("/api/providers/apply", {"name":"openai","api_key":"sk-test"}),
    ("/api/providers/test", {"name":"openai","base_url":"https://api.openai.com/v1","models":{"main":"gpt-4o-mini"}}),
]

print("="*70)
print("POST 端点（合法 payload）")
print("="*70)
for path, payload in POSTS:
    st, body = http(path, payload)
    if st == 200:
        keys = list(body)[:4] if isinstance(body, dict) else []
        print(f"  200  {path:34s} -> {keys}")
    else:
        msg = body.get("error") or body.get("_raw") or body.get("_exc") or body
        print(f"  {st}  {path:34s} -> {str(msg)[:90]}")

print()
print("="*70)
print("GET 端点")
print("="*70)
GETS = ["/api/status","/api/usage","/api/providers","/api/personas",
        "/api/conversations","/api/kb/list","/api/wiki/list","/api/tasks/list",
        "/api/skills/list","/api/runs","/api/voice/status","/api/tools/permissions",
        "/api/providers/models?name=openai","/api/wiki/page?name=probe"]
for path in GETS:
    st, body = http(path)
    if st == 200:
        keys = list(body)[:4] if isinstance(body, dict) else f"list[{len(body)}]"
        print(f"  200  {path:34s} -> {keys}")
    else:
        msg = body.get("error") or body.get("_raw") or body
        print(f"  {st}  {path:34s} -> {str(msg)[:90]}")

httpd.shutdown(); httpd.server_close()
