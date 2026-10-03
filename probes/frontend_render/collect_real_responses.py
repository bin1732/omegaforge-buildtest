#!/usr/bin/env python3
"""采真：起真后端 → 灌种子 → 跑真蒸馏 → 把每个接口的**真实响应**落盘。

为什么必须有这一步：

    上一章发现渲染检查脚本喂的是**手工构造的"真实响应形状"**——臆造了
    title / mode 等真实响应里根本不存在的字段。那种守卫会让"读 title
    的界面"也通过，而真实情况下那一栏是空的。
    **守卫用臆造输入，等于没验证输入侧。**

    本脚本把真后端抓下来的 JSON 原样落盘，交给渲染检查脚本消费。断言的
    不是"DOM 长什么样"，而是"真实响应里的那个具体值，出现在页面上"。

输出：/tmp/fe/real_responses.json —— { "<api path>": <真实响应> }
"""

import importlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.request

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
OUT = os.environ.get("FE_HOME", "/tmp/fe") + "/real_responses.json"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def http(path, payload=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw__": raw}


def main():
    tmp = "/tmp/real_seed_home"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = tmp
    os.environ["OMEGAFORGE_MODEL_MAIN"] = "mock-main"
    os.environ["OMEGAFORGE_MODEL_FAST"] = "mock-fast"
    os.environ["OMEGAFORGE_MODEL_JUDGE"] = "mock-judge"

    import omegaforge.server as srv
    from http.server import ThreadingHTTPServer

    for nm in ("RUNS", "CONVS", "USAGE", "TASKS", "KB", "WIKI", "JOBS", "MEMORY"):
        obj = getattr(srv, nm, None)
        if obj is not None and hasattr(obj, "home"):
            try:
                obj.home = tmp
            except Exception:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    out = {}
    try:
        # ---- 灌种子：十页都要有真实数据，空列表验不出字段契约 ----
        print("灌种子…")
        cid = http("/api/conversations/new", {"title": "联调会话：契约核对"})["id"]
        http("/api/chat", {"conversation_id": cid,
                           "message": "这条消息用于验证对话页渲染真实内容"})
        http("/api/kb/add", {"title": "联调知识条目",
                             "text": "契约必须来自真实响应，而非臆造形状",
                             "type": "note", "tags": ["联调", "契约"]})
        http("/api/kb/add", {"title": "第二条知识", "text": "验证列表能渲染多条",
                             "type": "note", "tags": []})
        http("/api/memory/remember", {"fact": "联调记忆：断言必须落在真实值上"})
        http("/api/tasks/add", {"text": "联调待办：核对十页字段契约", "priority": 2})
        http("/api/tasks/add", {"text": "联调待办二：验证完成态渲染", "priority": 1})
        http("/api/wiki/save", {"slug": "lian-tiao", "title": "联调词条",
                                "body": "词条正文，用于验证 wiki 列表与详情"})

        # 技能与人设：**空列表验不出字段契约**。上一版检验 /api/skills/list
        # 与 /api/personas 都是空，两页渲染守卫只能验"空态"，等于没验到
        # 真实字段。这里直接落盘两个技能包（一个普通、一个 type: persona），
        # 让列表与分类都走真实数据。
        # 导入即注册（模块级副作用），用 importlib 避免 pyflakes 报未使用
        importlib.import_module("omegaforge.skills.manager")
        sk_dir = os.path.join(tmp, "skills")
        for sub, fm in (
            ("code-audit", "name: code-audit\ndescription: 代码审计助手，逐行核对证据\ntype: tool\nversion: 1.0.0\nwhen_to_use: 需要审阅代码改动时\nrisk: 只读\n"),
            ("audit-persona", "name: audit-persona\ndescription: 严谨审计家人设，先核实再下结论\ntype: persona\nversion: 1.0.0\nwhen_to_use: 需要人设基调时\n"),
        ):
            d = os.path.join(sk_dir, sub)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\n" + fm + "---\n\n## 工作流\n\n1. 先核实证据\n2. 再下结论\n")

        # ---- 跑真蒸馏：RunsPage / ArenaPage / GenomePage 都要真产物 ----
        print("跑真蒸馏…")
        r = http("/api/distill", {
            "source": "你是一个严谨的代码审计助手，回答前必须先核实证据。",
            "budget": 20000, "rounds": 1, "gens": 1,
            "task": "联调验证：契约来自真实响应",
        })
        jid = str(r.get("job", ""))
        for _ in range(90):
            j = http(f"/api/jobs/{jid}", timeout=30)
            if str(j.get("status", "")) in ("done", "failed", "succeeded"):
                break
            time.sleep(1)
        print(f"  蒸馏完成 job={jid} status={j.get('status')}")

        # ---- 抓全部接口真实响应 ----
        print("抓真实响应…")
        paths = [
            "/api/status", "/api/conversations", "/api/kb/list",
            "/api/kb/search", "/api/tasks/list", "/api/wiki/list",
            "/api/skills/list", "/api/personas", "/api/usage",
            "/api/providers", "/api/providers/models?name=openai", "/api/runs",
            "/api/tools/permissions", "/api/tools/audit", "/api/voice/status",
            "/api/memory/recall",
        ]
        for p in paths:
            out[p] = http(p, timeout=30)
            print(f"  {p:32} keys={list(out[p])[:4] if isinstance(out[p], dict) else '?'}")

        rid = jid
        for p in (f"/api/jobs/{rid}", f"/api/report/{rid}", f"/api/genome/{rid}"):
            out[p] = http(p, timeout=30)
            print(f"  {p:32} keys={list(out[p])[:5]}")

        out["__run_id__"] = rid
        out["__conversation_id__"] = cid
    finally:
        httpd.shutdown()

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {OUT}  ({len(out)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
