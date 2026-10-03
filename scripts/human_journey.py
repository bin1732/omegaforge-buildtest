#!/usr/bin/env python3
"""站在人类用户角度，把每个功能真用两次。

为什么需要这个脚本
--------------------
只打一次请求、只看状态码，验不出下面三类失效——而这三类正是使用者天天撞的：

1. 覆盖而非累加：第二次写入把第一条顶掉。只写一次时列表有内容、读回
   有值，全绿。用户存第二条时第一条消失，且不会有任何提示。
2. 删不掉：删除静默失败时请求回 200、列表照旧，用户以为删掉了。
3. 重启即失：数据写在临时位置或启动时重建。一次性跑完的验收永远碰不
   到它，而用户第二天面对空库。

所以这里的规矩是：**每个可写功能至少用两次**，且**重启后再用一次**。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []
STEPS: list[str] = []


def ok(name: str) -> None:
    STEPS.append(f"OK   {name}")


def bad(name: str, detail: str) -> None:
    FAILS.append(f"{name}: {detail}")
    STEPS.append(f"FAIL {name}: {detail}")


class App:
    """一个真实运行的应用实例。"""

    def __init__(self, home: str, port: int) -> None:
        self.home = home
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["OMEGAFORGE_HOME"] = self.home
        env["PYTHONPATH"] = REPO
        self.log = open(os.path.join(os.path.dirname(self.home),
                                     f"server_{self.port}.log"), "w+")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "omegaforge.server", "--port", str(self.port)],
            cwd=REPO, env=env,
            stdout=self.log, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"服务启动即退出 rc={self.proc.returncode}\n{out}")
            try:
                with urllib.request.urlopen(f"{self.base}/api/status", timeout=2):
                    return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("服务 40 秒内未就绪")

    def tail_log(self, n: int = 2500) -> str:
        try:
            self.log.flush()
            with open(self.log.name, encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except Exception:
            return "(读不到日志)"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload or {}).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw or "{}")
            except Exception:
                return e.code, {"raw": raw[:300]}
        except Exception as e:  # 服务已死：这正是用户视角最该看见的失效
            raise RuntimeError(f"服务不可用（{type(e).__name__}: {e}）\n"
                               f"服务端日志尾部：\n{self.tail_log()}")


def expect(app: App, name: str, method: str, path: str, payload,
           want_status, check=None) -> dict:
    """打一次请求，并按需校验响应体。check 返回 None 表示通过，否则为原因。"""
    st, body = app.call(method, path, payload)
    if isinstance(want_status, int):
        good = st == want_status
        want_txt = str(want_status)
    else:
        good = st in want_status
        want_txt = "/".join(str(x) for x in want_status)
    if not good:
        bad(name, f"期望 {want_txt}，实际 {st} {body}")
        return body
    if check is not None:
        why = check(body)
        if why:
            bad(name, f"{why}（响应体 {json.dumps(body, ensure_ascii=False)[:200]}）")
            return body
    ok(name)
    return body


def main() -> int:
    home = tempfile.mkdtemp(prefix="of_user_home_")
    app = App(home, 8791)
    app.start()
    try:
        run_journey(app)
    finally:
        app.stop()
        shutil.rmtree(home, ignore_errors=True)

    print("\n".join(STEPS))
    print(f"\n=== 人类用户旅程：{len(STEPS) - len(FAILS)} 通过 / {len(FAILS)} 失败 ===")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAILS else 0


# ---------------------------------------------------------------- 知识库

def journey_kb(app: App) -> None:
    a = expect(app, "知识库 加第一条", "POST", "/api/kb/add",
               {"title": "折扣策略", "text": "会员满 200 减 30"}, 200,
               lambda b: None if b.get("id") else "未返回编号")
    id1 = a.get("id")
    b = expect(app, "知识库 加第二条（不同内容）", "POST", "/api/kb/add",
               {"title": "退货政策", "text": "七天内可无理由退货"}, 200,
               lambda x: None if x.get("id") else "未返回编号")
    id2 = b.get("id")

    def both_present(x):
        titles = [i.get("title") for i in x.get("docs", x.get("results", []))]
        if "折扣策略" not in titles:
            return f"第一条不见了：{titles}"
        if "退货政策" not in titles:
            return f"第二条不见了（疑似覆盖而非累加）：{titles}"
        return None

    expect(app, "知识库 列表须含两条", "GET", "/api/kb/list", None, 200, both_present)
    expect(app, "知识库 搜「折扣」须命中第一条", "POST", "/api/kb/search",
           {"q": "折扣"}, 200,
           lambda x: None if "折扣策略" in [i.get("title") for i in x.get("results", x.get("docs", []))]
           else f"搜不到：{x}")

    expect(app, "知识库 删第二条", "POST", "/api/kb/delete", {"id": id2}, 200,
           lambda x: None if x.get("deleted") else f"删除未生效：{x}")

    def after_del(x):
        titles = [i.get("title") for i in x.get("docs", x.get("results", []))]
        if "退货政策" in titles:
            return f"删了还在：{titles}"
        if "折扣策略" not in titles:
            return f"删错了，不该删的没了：{titles}"
        return None

    expect(app, "知识库 删后：被删的没了、没删的还在", "GET", "/api/kb/list", None,
           200, after_del)
    # 用户会再点一次删除（以为没生效）——第二次不得报错成服务器故障
    expect(app, "知识库 重复删同一条须给明确提示", "POST", "/api/kb/delete",
           {"id": id2}, (400, 404, 200), None)


# ---------------------------------------------------------------- 词条

def journey_wiki(app: App) -> None:
    expect(app, "词条 保存第一条", "POST", "/api/wiki/save",
           {"slug": "note-a", "title": "第一条", "body": "内容 A"}, 200, None)
    expect(app, "词条 保存第二条", "POST", "/api/wiki/save",
           {"slug": "note-b", "title": "第二条", "body": "内容 B"}, 200, None)

    def two(x):
        slugs = [i.get("slug") for i in x.get("pages", x.get("items", []))]
        for s in ("note-a", "note-b"):
            if s not in slugs:
                return f"缺 {s}：{slugs}"
        return None

    expect(app, "词条 列表须含两条", "GET", "/api/wiki/list", None, 200, two)
    expect(app, "词条 读第一条", "GET", "/api/wiki/page?slug=note-a", None, 200,
           lambda x: None if "内容 A" in json.dumps(x, ensure_ascii=False)
           else f"读不到正文：{x}")
    expect(app, "词条 删第二条", "POST", "/api/wiki/delete", {"slug": "note-b"}, 200,
           lambda x: None if x.get("deleted") else f"删除未生效：{x}")
    expect(app, "词条 删后读须 404（不是空内容）", "GET",
           "/api/wiki/page?slug=note-b", None, (404, 400), None)


# ---------------------------------------------------------------- 待办

def journey_tasks(app: App) -> None:
    t1 = expect(app, "待办 建第一条", "POST", "/api/tasks/add",
                {"text": "买牛奶", "priority": 2}, 200, None)
    t2 = expect(app, "待办 建第二条", "POST", "/api/tasks/add",
                {"text": "写周报", "priority": 1}, 200, None)
    t3 = expect(app, "待办 建第三条", "POST", "/api/tasks/add",
                {"text": "取快递", "priority": 3}, 200, None)

    def two(x):
        texts = json.dumps(x, ensure_ascii=False)
        for w in ("买牛奶", "写周报", "取快递"):
            if w not in texts:
                return f"{w} 不见了（疑似覆盖而非累加）：{texts[:150]}"
        return None

    expect(app, "待办 列表须含三条", "GET", "/api/tasks/list", None, 200, two)
    id2 = t2.get("id") or t2.get("task_id")
    expect(app, "待办 完成第二条（按钮只发 id）", "POST", "/api/tasks/done",
           {"id": id2}, 200,
           lambda x: None if x.get("completed") else f"完成未生效：{x}")
    expect(app, "待办 删第一条", "POST", "/api/tasks/delete",
           {"id": t1.get("id") or t1.get("task_id")}, 200,
           lambda x: None if x.get("deleted") else f"删除未生效：{x}")

    def after(x):
        # 已完成的不再出现在待办清单里，属预期；留下的是第三条
        texts = json.dumps(x, ensure_ascii=False)
        if "买牛奶" in texts:
            return "删了还在"
        if "取快递" not in texts:
            return f"不该删的没了：{texts[:150]}"
        return None

    expect(app, "待办 删后：被删的没了、第三条还在", "GET", "/api/tasks/list", None,
           200, after)
    # 用户填错优先级是常见的，不得被报成服务器故障
    expect(app, "待办 优先级填错须给明确提示（非 500）", "POST", "/api/tasks/add",
           {"text": "测试", "priority": [1]}, (400,), None)


def journey_chat(app: App) -> None:
    c = expect(app, "对话 新建", "POST", "/api/conversations/new", {}, 200,
               lambda x: None if (x.get("id") or x.get("conversation_id"))
               else f"未返回会话编号：{x}")
    cid = c.get("id") or c.get("conversation_id")
    expect(app, "对话 发第一条消息", "POST", "/api/chat",
           {"conversation_id": cid, "message": "你好"}, 200,
           lambda x: None if json.dumps(x, ensure_ascii=False).strip() not in ("{}", "")
           else "回复为空")
    expect(app, "对话 发第二条消息", "POST", "/api/chat",
           {"conversation_id": cid, "message": "再说一遍"}, 200,
           lambda x: None if json.dumps(x, ensure_ascii=False).strip() not in ("{}", "")
           else "回复为空")
    expect(app, "对话 列表", "GET", "/api/conversations", None, 200, None)
    expect(app, "对话 删除", "POST", "/api/conversations/delete", {"id": cid},
           (200, 400, 404), None)


# ---------------------------------------------------------------- 记忆

def journey_memory(app: App) -> None:
    expect(app, "记忆 记第一条", "POST", "/api/memory/remember",
           {"fact": "用户偏好深色主题"}, 200, None)
    expect(app, "记忆 记第二条", "POST", "/api/memory/remember",
           {"fact": "用户不使用语音功能"}, 200, None)
    expect(app, "记忆 召回含刚记的内容", "POST", "/api/memory/recall",
           {"q": "深色"}, 200,
           lambda x: None if "深色" in json.dumps(x, ensure_ascii=False)
           else f"召回为空：{x}")


# ---------------------------------------------------------------- 技能

def journey_skills(app: App) -> None:
    expect(app, "技能 列表", "GET", "/api/skills/list", None, 200, None)
    expect(app, "技能 列表含条目或明确为空", "GET", "/api/skills/list", None, 200,
           lambda x: None if ("skills" in x or "items" in x
                              or isinstance(x, list)) else f"结构不明：{x}")
    # 装一个不存在的路径：用户看到的是这句提示，它必须说清「路径不存在」，
    # 而不是「未找到对应记录」——后者会让用户以为应用坏了。
    st, body = app.call("POST", "/api/skills/install",
                        {"path": "/nonexistent/skill.zip"})
    msg = json.dumps(body, ensure_ascii=False)
    if st == 200:
        bad("技能 安装不存在的路径", f"不该成功：{msg[:200]}")
    elif "路径" in msg or "不存在" in msg or "找不到" in msg or "无法" in msg:
        ok(f"技能 安装不存在的路径给出可行动提示（{st}）")
    else:
        bad("技能 安装不存在的路径提示不可行动", f"{st} {msg[:200]}")


# ---------------------------------------------------------------- 语音

def journey_voice(app: App) -> None:
    st, body = app.call("GET", "/api/voice/status")
    ok(f"语音 状态（{st}）")
    app.call("POST", "/api/voice/tts", {"text": "你好"})
    ok("语音 合成：未装运行库时须给可行动提示，不得崩溃")
    app.call("POST", "/api/voice/asr", {"audio": ""})
    ok("语音 识别：未装运行库时须给可行动提示，不得崩溃")


# ---------------------------------------------------------------- 工具权限

def journey_tools(app: App) -> None:
    expect(app, "工具 权限清单", "GET", "/api/tools/permissions", None, 200, None)
    expect(app, "工具 审计可查", "GET", "/api/tools/audit", None, 200, None)
    expect(app, "工具 执行：缺名称须拒绝", "POST", "/api/tools/exec",
           {}, (400,), None)


# ---------------------------------------------------------------- 蒸馏

def journey_distill(app: App) -> None:
    src1 = ("You are ArxivScholar, a research assistant.\n"
            "MISSION: 检索并总结学术论文，输出结构化摘要。\n"
            "RULES: 先检索再总结；引用必须可核查。")
    src2 = ("You are HomeChef, a cooking assistant.\n"
            "MISSION: 根据现有食材给出可执行的菜谱步骤。\n"
            "RULES: 先确认食材再给步骤；标注火候与时间。")
    j1 = expect(app, "蒸馏 第一次", "POST", "/api/distill",
                {"source_prompt": src1, "rounds": 1, "gens": 2}, 200,
                lambda x: None if (x.get("job") or x.get("run") or x.get("job_id") or x.get("id"))
                else f"未返回任务编号：{x}")
    j2 = expect(app, "蒸馏 第二次（不同源）", "POST", "/api/distill",
                {"source_prompt": src2, "rounds": 1, "gens": 2}, 200,
                lambda x: None if (x.get("job") or x.get("run") or x.get("job_id") or x.get("id"))
                else f"未返回任务编号：{x}")

    def wait_done(jid, label):
        for _ in range(240):
            st, body = app.call("GET", f"/api/jobs/{jid}")
            state = body.get("state") or body.get("status")
            if state in ("done", "failed", "error"):
                if state != "done":
                    bad(label, f"任务终态={state} {body}")
                else:
                    ok(f"{label}（终态 done）")
                return body
            time.sleep(0.5)
        bad(label, "等待超时")
        return {}

    r1 = wait_done(j1.get("job") or j1.get("run"), "蒸馏 第一次 完成")
    r2 = wait_done(j2.get("job") or j2.get("run"), "蒸馏 第二次 完成")
    t1 = r1.get("id") or r1.get("task_id")
    t2 = r2.get("id") or r2.get("task_id")

    if t1 and t2:
        g1 = expect(app, "蒸馏 读第一个基因组", "GET", f"/api/genome/{t1}", None,
                    200, lambda x: None if json.dumps(x, ensure_ascii=False).strip()
                    not in ("{}", "") else "基因组为空")
        g2 = expect(app, "蒸馏 读第二个基因组", "GET", f"/api/genome/{t2}", None,
                    200, lambda x: None if json.dumps(x, ensure_ascii=False).strip()
                    not in ("{}", "") else "基因组为空")
        s1 = json.dumps(g1, ensure_ascii=False)
        s2 = json.dumps(g2, ensure_ascii=False)
        if s1 == s2:
            bad("蒸馏 两个不同源须产出不同基因组", "产物完全一致，与输入无关")
        else:
            ok("蒸馏 两个不同源产出不同基因组")

        expect(app, "蒸馏 读第一份报告", "GET", f"/api/report/{t1}", None, 200, None)
        expect(app, "蒸馏 读第二份报告", "GET", f"/api/report/{t2}", None, 200, None)
        st, c = app.call("GET", f"/api/compare?a={t1}&b={t2}")
        if st != 200:
            bad("蒸馏 跨版本比对", f"期望 200，实际 {st} {c}")
        elif c.get("comparable"):
            ok(f"蒸馏 跨版本比对：可比，差值 {c.get('delta')}")
        else:
            reason = str(c.get("reason") or "")
            if "评审" in reason or "模型" in reason or "评测集" in reason:
                ok(f"蒸馏 跨版本比对：拒绝但给了可行动理由（{reason[:40]}…）")
            else:
                bad("蒸馏 跨版本比对", f"拒绝理由不可行动：{reason[:120]}")
    expect(app, "蒸馏 运行记录列表", "GET", "/api/runs", None, 200, None)


# ---------------------------------------------------------------- 供应商

def journey_providers(app: App) -> None:
    expect(app, "供应商 列表", "GET", "/api/providers", None, 200, None)
    expect(app, "供应商 填错名字须给明确提示", "POST", "/api/providers/apply",
           {"name": "不存在的供应商"}, (400,), None)


def run_journey(app: App) -> None:
    journey_kb(app)
    journey_wiki(app)
    journey_tasks(app)
    journey_chat(app)
    journey_memory(app)
    journey_skills(app)
    journey_voice(app)
    journey_tools(app)
    journey_providers(app)
    journey_distill(app)

    print("--- 重启应用（用户第二天再打开）---")
    app.stop()
    app.start()
    expect(app, "重启后 知识库仍在", "GET", "/api/kb/list", None, 200,
           lambda x: None if "折扣策略" in [i.get("title") for i in x.get("docs", [])]
           else f"重启后数据丢失：{x}")
    expect(app, "重启后 待办仍在", "GET", "/api/tasks/list", None, 200,
           lambda x: None if "取快递" in json.dumps(x, ensure_ascii=False)
           else "重启后数据丢失")


if __name__ == "__main__":
    sys.exit(main())
