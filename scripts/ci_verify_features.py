# -*- coding: utf-8 -*-
"""对"安装后真实运行的应用"逐个功能发真实请求，判定是不是空壳。

## 与 ci_verify_install.py 的分工

ci_verify_install.py 只回答"装完能不能启动、布局对不对"。它能通过，
而每个端点仍可能返回空列表、HTML 错误页或占位假值——启动成功和功能
可用是两件事。本脚本回答后者。

## 判定口径

每条端点必须同时满足：
  1. 路由真的存在（不是 404 / 405 / 501）
  2. 请求被服务端**接住**（2xx，而不是 4xx 配一段结构化错误正文）
  3. 响应体是 JSON 而不是 HTML 错误页
  4. 响应体非空
  5. 写入过的东西能读回来（期望串出现在后续响应里）

第 2 条是入参契约漂移唯一的防线：字段改名后每个写端点都回 400，而
400 配合法 JSON 正文能同时满足 1、3、4，只留前三条会全绿。
第 5 条把"端点答了话"与"功能真的生效"分开——写入请求回 200 但没落盘，
只看单条响应是发现不了的。

## 为什么必须对"安装后"的实例发请求

源码目录里跑后端用的是开发态数据与开发态路径。用户拿到的是安装包
解出来的目录：数据目录、资源相对位置、sidecar 的工作目录都不同。
只有对安装目录里那个 exe 发请求，才等价于用户双击应用后前端发起的
调用。

用法：
  python scripts/ci_verify_features.py <安装目录> [端口]
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIAG = os.path.join(ROOT, "ci_diag")
SIDECAR = "omegaforge-backend"

# 验收步骤：**有序**。只读端点对应用户打开的每个页面首屏；写入端点对应
# 用户真的一次操作，且后面跟着"读回"——写完读不到，等于功能没生效，而只
# 发一次写请求是看不出来的。
#
# 每条为 (名字, 方法, 路径, 入参, 期望)：
#   * 入参里的 "$<步骤名>.<字段>" 引用前面某步的返回值，用于"写完再读回"。
#     任务完成要拿新增返回的任务编号，手写常量会在数据变化后恒失败。
#   * 期望为 None 表示只要求"接住且非空"；给出字符串时要求响应原文里含它
#     ——这是把"端点答了话"与"功能真的生效"分开的那一条。
STEPS = [
    ("status", "GET", "/api/status", None, None),
    ("usage", "GET", "/api/usage", None, None),
    ("tools-permissions", "GET", "/api/tools/permissions", None, None),
    ("tools-policy", "GET", "/api/tools/policy", None, None),
    ("tools-audit", "GET", "/api/tools/audit", None, None),
    ("providers", "GET", "/api/providers", None, None),
    # 供应商型号要带 name：不带时服务端回 404「未找到该模型供应商」，
    # 看着像功能坏了，实际是清单漏了必填入参。
    ("providers-models", "GET", "/api/providers/models?name=openai",
     None, None),
    ("personas", "GET", "/api/personas", None, None),
    ("conversations", "GET", "/api/conversations", None, None),
    ("kb-list", "GET", "/api/kb/list", None, None),
    ("wiki-list", "GET", "/api/wiki/list", None, None),
    ("tasks-list", "GET", "/api/tasks/list", None, None),
    ("skills-list", "GET", "/api/skills/list", None, None),
    ("runs", "GET", "/api/runs", None, None),
    ("voice-status", "GET", "/api/voice/status", None, None),

    ("kb-add", "POST", "/api/kb/add",
     {"text": "验收写入的一条知识", "title": "验收"}, None),
    ("kb-search", "POST", "/api/kb/search", {"query": "验收写入的一条知识"},
     "验收写入的一条知识"),
    ("kb-list-after", "GET", "/api/kb/list", None, "验收写入的一条知识"),

    ("wiki-save", "POST", "/api/wiki/save",
     {"slug": "verify", "title": "验收词条", "body": "验收正文"}, None),
    # 词条详情只认 GET 查询串：POST 过去是 404 接口不存在。
    ("wiki-page", "GET", "/api/wiki/page?slug=verify", None, "验收词条"),

    ("tasks-add", "POST", "/api/tasks/add",
     {"text": "验收待办", "priority": 2}, None),
    ("tasks-done", "POST", "/api/tasks/done", {"task_id": "$tasks-add.id"},
     None),
    ("tasks-done-list", "GET", "/api/tasks/list?scope=done", None, "验收待办"),

    # 记忆收的是 fact 不是 text：写错时 400 + 结构化正文，旧判定照样判 PASS。
    ("memory-remember", "POST", "/api/memory/remember",
     {"fact": "验收记住的一条事实"}, None),
    ("memory-recall", "POST", "/api/memory/recall",
     {"query": "验收记住的一条事实"}, "验收记住的一条事实"),

    ("conversations-new", "POST", "/api/conversations/new",
     {"title": "验收会话"}, None),
    ("conversations-after", "GET", "/api/conversations", None, "验收会话"),
]


# 第二轮复用步骤：每个写功能至少用两次。
#
# 只用一次验不出两类失效，而它们都在用户日常里：
#   * 覆盖而非累加 —— 第二次写入把第一条顶掉，列表里只剩最新一条。
#     只写一次时列表里有内容、读回有值，全绿。
#   * 删不掉 —— 删除端点缺失或静默失败时，加错的东西永远留在列表里。
#     存储层的 delete() 早已存在而 HTTP 缺出口时正是这个形态。
#
# 每条为 (名字, 方法, 路径, 入参, 期望含, 期望不含)。后两者给期望值即可，
# 缺失时 None 表示不校验该维度。
REUSE_STEPS = [
    ("kb-add-2", "POST", "/api/kb/add",
     {"text": "第二轮写入的知识", "title": "第二轮"}, None, None),
    # 两条都在：只出现第二条即说明写入是覆盖而非累加。
    ("kb-list-2", "GET", "/api/kb/list", None,
     "验收写入的一条知识", None),
    ("kb-list-2b", "GET", "/api/kb/list", None, "第二轮写入的知识", None),
    ("kb-delete-2", "POST", "/api/kb/delete", {"doc_id": "$kb-add-2.id"},
     None, None),
    # 删掉之后：第二条必须真的没了，第一条必须还在（删错目标也报出来）。
    ("kb-list-after-del", "GET", "/api/kb/list", None,
     "验收写入的一条知识", None),
    ("kb-list-after-del-b", "GET", "/api/kb/list", None, None,
     "第二轮写入的知识"),

    ("wiki-save-2", "POST", "/api/wiki/save",
     {"slug": "verify2", "title": "第二轮词条", "body": "第二轮正文"},
     None, None),
    ("wiki-list-2", "GET", "/api/wiki/list", None, "verify", None),
    ("wiki-list-2b", "GET", "/api/wiki/list", None, "verify2", None),
    ("wiki-delete-2", "POST", "/api/wiki/delete", {"slug": "verify2"},
     None, None),
    # 删除后用列表判定而不是取详情：详情的 404 既可能是"词条没了"，
    # 也可能是"路由不存在"，两者状态码相同，分不开。
    ("wiki-list-after-del", "GET", "/api/wiki/list", None, "verify", None),
    ("wiki-list-after-del-b", "GET", "/api/wiki/list", None, None,
     "verify2"),

    ("tasks-add-2", "POST", "/api/tasks/add",
     {"text": "第二轮待办", "priority": 3}, None, None),
    # 第一条在功能级已被标记完成：默认列表只列 pending，
    # 查它必须走 scope=done，否则会把"已完成的不在待办列表里"
    # 误读成"数据丢了"。
    ("tasks-list-2", "GET", "/api/tasks/list?scope=done", None,
     "验收待办", None),
    ("tasks-list-2b", "GET", "/api/tasks/list", None, "第二轮待办", None),
    ("tasks-delete-2", "POST", "/api/tasks/delete",
     {"task_id": "$tasks-add-2.id"}, None, None),
    ("tasks-list-after-del", "GET", "/api/tasks/list?scope=done",
     None, "验收待办", None),
    ("tasks-list-after-del-b", "GET", "/api/tasks/list", None, None,
     "第二轮待办"),
]

# 重启后仍在：用户关掉应用再打开，数据必须在。
# 这是"第二次使用"最常见的形态，而一次性跑完的验收永远碰不到它。
# 每条为 (名字, 方法, 路径, 入参, 期望正文片段)。
RESTART_CHECKS = [
    ("kb-after-restart", "GET", "/api/kb/list", None,
     "验收写入的一条知识"),
    ("wiki-after-restart", "GET", "/api/wiki/page?slug=verify", None,
     "验收词条"),
    ("memory-after-restart", "POST", "/api/memory/recall",
     {"query": "验收记住的一条事实"}, "验收记住的一条事实"),
    ("tasks-after-restart", "GET", "/api/tasks/list?scope=done",
     None, "验收待办"),
]


# 契约级步骤：这些端点无法在验收环境里跑完整功能（要真实模型、音频、
# 长流水线或已完成的运行），但**可以验它们的契约还在不在**——
# 用空入参打过去，端点应当给出自己那条明确提示，而不是"接口不存在"。
#
# 为什么这一层不能省：功能级覆盖不到的那些端点若一个都不请求，
# 验收报告却照写"全部通过"，读起来像每个功能都验过了。
# 路由被改删、字段名漂移、提示文案被换掉，这三类都会让功能在界面上失效，
# 而它们全部落在"没被请求过"的那部分里。
#
# 每条为 (名字, 方法, 路径, 入参, 期望状态码, 期望正文片段)。
REACH_STEPS = [
    ("distill", "POST", "/api/distill", {}, 400, "请先粘贴源 Agent 的系统提示词"),
    ("chat", "POST", "/api/chat", {}, 400, "请输入消息内容"),
    ("chat-stream", "POST", "/api/chat/stream", {}, 400, "请输入消息内容"),
    ("compare", "GET", "/api/compare", None, 400, "请提供要比对的两个任务编号"),
    ("jobs", "GET", "/api/jobs/__verify__", None, 404, "未找到该任务"),
    ("genome", "GET", "/api/genome/__verify__", None, 404, "未找到该任务"),
    ("report", "GET", "/api/report/__verify__", None, 404, "未找到该任务"),
    ("conversations-delete", "POST", "/api/conversations/delete", {}, 400,
     "请填写：对话"),
    ("conversations-model", "POST", "/api/conversations/model", {}, 400,
     "请填写：对话"),
    ("voice-tts", "POST", "/api/voice/tts", {}, 400, "要朗读的文字"),
    ("voice-asr", "POST", "/api/voice/asr", {}, 400, "未提供音频数据"),
    # 一键安装与进度查询：空入参须在校验处返回，不得吞成通用提示。
    # 新增端点必须同步登记进本清单：覆盖面守卫对未探测的端点一律报失败，
    # 路由改删与提示漂移才不会静默发生。
    ("voice-models-install", "POST", "/api/voice/models/install", {}, 400,
     "请填写：模型类型"),
    ("voice-models-progress", "GET", "/api/voice/models/progress", None, 400,
     "请填写：模型类型"),
    ("tools-exec", "POST", "/api/tools/exec", {}, 400, "工具名称"),
    ("kb-delete", "POST", "/api/kb/delete", {}, 400, "知识编号"),
    ("wiki-delete", "POST", "/api/wiki/delete", {}, 400, "词条标识"),
    ("tasks-delete", "POST", "/api/tasks/delete", {}, 400, "任务编号"),
    ("skills-install", "POST", "/api/skills/install", {}, 400, "技能包路径"),
    ("skills-invoke", "POST", "/api/skills/invoke", {}, 400, "技能名称"),
    ("providers-apply", "POST", "/api/providers/apply", {}, 400, "模型供应商"),
    ("providers-test", "POST", "/api/providers/test", {}, 400, "必填项"),
]

# 既不验功能、也不验契约的端点：必须登记理由，否则验收会静默少验。
UNPROBED: dict[str, str] = {}


def reach_verdict(lines: list[str], name: str, path: str, status: int,
                  body: str, want_status: int, want_text: str) -> bool:
    """契约级判定：路由在、且给出的仍是它自己那条提示。

    只看状态码不够——路由被删时同样回 404（"接口不存在"），
    与"任务不存在"的 404 无法区分，必须靠正文片段。
    """
    if not want_text.strip():
        # 空期望会让 `want_text in body` 恒真：契约级步骤全部通过而什么都没验。
        # 这类退化没有别的症状，故在判定处直接判失败。
        log(lines, f"  [FAIL] {name:<20} 期望正文为空 —— 该条等于没验 {path}")
        return False
    if "接口不存在" in body:
        log(lines, f"  [FAIL] {name:<20} 路由已不存在 {path}")
        return False
    if status != want_status:
        log(lines, f"  [FAIL] {name:<20} 状态码 {status} ≠ 期望 {want_status} "
                   f"{body[:80]!r} {path}")
        return False
    if want_text not in body:
        log(lines, f"  [FAIL] {name:<20} 契约提示已变，期望含 {want_text!r} "
                   f"实际 {body[:120]!r} {path}")
        return False
    log(lines, f"  [PASS] {name:<20} {status}  契约在  {path}")
    return True


# 核心流水线：蒸馏 → 基因组 → 报告 → 跨版本比对，以及对话。
#
# 为什么必须有这一段：上面 27 条功能级里，`/api/distill` 只发过空入参。
# 也就是说旗舰功能在装机实例上从未真正跑过一次——路由在、校验在、
# 报告全绿，而"点开始蒸馏能不能吐出基因组"无人能证。
#
# 离线可跑的前提：未配置 key 且 base_url 为默认值时后端走纯离线变换
# （见 LLMClient.mock_mode），不联网、不产生费用。故这里要求实例处于
# 离线态再跑；若已配置真实模型，则不做——真实调用会花钱，且结果不具
# 可重复性。
SOURCE_A = (
    "You are ArxivResearcher, a meticulous academic research assistant. "
    "Your mission: locate and summarize academic papers with rigor and "
    "precision. Always cite sources with arXiv IDs. Never fabricate DOIs. "
    "Workflow: parse intent -> search -> filter -> read abstracts -> "
    "synthesize. Output format: markdown brief with a sources section.")
SOURCE_B = (
    "You are CodeReviewer. Review code for correctness and security. "
    "Workflow: read diff -> check edge cases -> report findings. "
    "Output: prioritized list of issues with severity.")


def _poll_job(lines: list[str], port: int, jid: str, budget_s: int = 180):
    """轮询到终态。返回 (终态, 详情)。"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < budget_s:
        st, body = call("GET", port, f"/api/jobs/{jid}", None)
        try:
            last = json.loads(body)
        except json.JSONDecodeError:
            last = {}
        if last.get("status") == "done":
            return "done", last
        if last.get("status") in ("failed", "error"):
            return "failed", last
        time.sleep(3)
    log(lines, f"  [FAIL] 任务 {jid} 在 {budget_s}s 内未到终态：{str(last)[:200]}")
    return "timeout", last


def run_realuse(lines: list[str], port: int) -> tuple[int, int]:
    """把技能 / 会话 / 工具逐个真实使用两次。

    契约级只证明"路由还在、提示没变"，不证明"用户点下去能成"。这三类
    功能在只发空入参时必须视为未验收：它们同样回 400 且是合法 JSON，
    与真实可用在报告上无法区分。

    技能包必须造在临时目录：验收自己写进安装目录的文件会混进"卸载后
    还剩什么"的测量，真正该报的运行时残留反而被淹没。
    """
    total = 0
    passed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal total, passed
        total += 1
        if ok:
            passed += 1
            log(lines, f"  [PASS] {name:<22} {detail}")
        else:
            log(lines, f"  [FAIL] {name:<22} {detail}")

    def jcall(method: str, path: str, payload=None) -> tuple[int, str, dict]:
        st, body = call(method, port, path, payload)
        try:
            d = json.loads(body)
        except Exception:
            d = {}
        return st, body, (d if isinstance(d, dict) else {})

    # ---- 会话：真实新建两次、切模型、删掉一个 ----
    st, _, d1 = jcall("POST", "/api/conversations/new", {"title": "真实使用甲"})
    cid1 = str(d1.get("id", ""))
    check("会话-新建甲", st == 200 and bool(cid1), f"id={cid1} ({st})")
    st, _, d2 = jcall("POST", "/api/conversations/new", {"title": "真实使用乙"})
    cid2 = str(d2.get("id", ""))
    check("会话-新建乙", st == 200 and bool(cid2), f"id={cid2} ({st})")
    if cid1 and cid2:
        check("会话-两条都在", cid1 != cid2, "两个编号不同")
        st, body, _ = jcall("GET", "/api/conversations")
        check("会话-列表含两条",
              "真实使用甲" in body and "真实使用乙" in body, f"({st})")
        st, body, _ = jcall("POST", "/api/conversations/model",
                            {"id": cid2, "model": "mock-main"})
        check("会话-切换模型", st == 200 and "mock-main" in body, f"({st})")
        st, body, _ = jcall("POST", "/api/conversations/delete", {"id": cid2})
        check("会话-删除乙", st == 200, f"({st})")
        st, body, _ = jcall("GET", "/api/conversations")
        check("会话-删后只剩甲",
              "真实使用甲" in body and "真实使用乙" not in body, f"({st})")

    # ---- 技能：真实安装、调用两次、覆盖安装 ----
    pkg = os.path.join(tempfile.gettempdir(), "of_realuse_skill")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: realuse-notes\ndescription: 整理要点\n---\n"
                "整理以下内容：\n{{context}}\n输出要点列表。\n")
    st, body, d = jcall("POST", "/api/skills/install", {"path": pkg})
    check("技能-安装", st == 200 and "realuse-notes" in body, f"({st})")
    st, body, _ = jcall("GET", "/api/skills/list")
    check("技能-列表含它", "realuse-notes" in body, f"({st})")
    _, b1, _ = jcall("POST", "/api/skills/invoke",
                     {"name": "realuse-notes", "context": "第一次会议"})
    _, b2, _ = jcall("POST", "/api/skills/invoke",
                     {"name": "realuse-notes", "context": "第二次会议"})
    # 两次调用必须带各自的上下文：返回同一份就说明 context 根本没进去。
    check("技能-调用第一次", "第一次会议" in b1, "上下文已渲染")
    check("技能-调用第二次", "第二次会议" in b2, "上下文已渲染")
    check("技能-两次结果不同", b1 != b2, "随上下文变化")
    st, body, _ = jcall("POST", "/api/skills/install", {"path": pkg})
    check("技能-覆盖安装", st == 200, f"({st})")
    st, body, _ = jcall("GET", "/api/skills/list")
    check("技能-覆盖后仍在", "realuse-notes" in body, f"({st})")

    # ---- 工具：能力开关 + 真实执行两次 ----
    st, body, d = jcall("POST", "/api/tools/permissions", {"fs": True})
    # 必须判定 fs 这一项真的为 true：只数 body 里有几个 true 的话，
    # 开启失败而别的项恰好为真时照样判通过——等于没验。
    got = d.get("permissions")
    check("能力-开启文件",
          st == 200 and isinstance(got, dict) and got.get("fs") is True,
          f"({st}) {got}")
    _, body, d = jcall("POST", "/api/tools/exec",
                       {"name": "fs_list", "arguments": {"path": "."}})
    listed = isinstance(d.get("items"), list)
    check("工具-真实执行首次", listed, f"items={len(d.get('items') or [])}")
    _, _, d2b = jcall("POST", "/api/tools/exec",
                      {"name": "fs_list", "arguments": {"path": "."}})
    check("工具-真实执行二次", isinstance(d2b.get("items"), list),
          f"items={len(d2b.get('items') or [])}")
    # 收尾关回，避免把验收环境留在高权限态。
    jcall("POST", "/api/tools/permissions", {"fs": False})

    # ---- 权限级别：标识与中文名都必须可用 ----
    st, body, _ = jcall("POST", "/api/tools/policy", {"mode": "full"})
    check("权限级别-用标识", st == 200, f"({st})")
    st, body, _ = jcall("POST", "/api/tools/policy", {"mode": "变更前确认"})
    check("权限级别-用中文名", st == 200,
          "照报错填中文名也必须能设，否则用户重试多少次都失败")

    # ---- 语音：内置模型必须真的能出声（站用户角度用两次）----
    #
    # 只验 status 是不够的：status 说 ready 只代表文件在、体积够，不代表
    # 模型能加载、能合成。而"合成接口返回 200 但音频是空的"在只查状态码
    # 时也判通过——用户听到的是一片寂静。因此这里真的解出 WAV 并校验。
    def synth(text: str):
        st, _, d = jcall("POST", "/api/voice/tts", {"text": text, "speed": 1.0})
        if st != 200:
            return None, f"({st})"
        b64 = d.get("audio_b64") or ""
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None, "音频解码失败"
        # WAV 头校验：RIFF....WAVE
        if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            return None, f"不是合法 WAV（{len(raw)} 字节）"
        # 非静音校验：全零负载说明合成出了空音频，接口仍回 200。
        payload = raw[44:]
        if not any(payload):
            return None, "音频内容全为零（静音）"
        return raw, f"{len(raw)} 字节 / {d.get('duration_s')} 秒"

    st, _, dv = jcall("GET", "/api/voice/status")
    asr_st = dv.get("asr") or {}
    tts_st = dv.get("tts") or {}
    a_ready = bool(asr_st.get("ready"))
    t_ready = bool(tts_st.get("ready"))
    check("语音-内置就绪", a_ready and t_ready,
          f"asr缺={asr_st.get('files_missing')} 库={asr_st.get('lib_installed')} "
          f"tts缺={tts_st.get('files_missing')} 库={tts_st.get('lib_installed')}")
    if a_ready and t_ready:
        w1, i1 = synth("欢迎使用万象工坊，这是第一次合成")
        check("语音-合成首次", w1 is not None, i1)
        w2, i2 = synth("这是第二次合成，内容与前一次不同")
        check("语音-合成二次", w2 is not None, i2)
        check("语音-两次产物不同", w1 is not None and w2 is not None and w1 != w2,
              "随文本变化；相同说明固定返回同一段")
        if w1 is not None:
            # 闭环：合成出来的音频交给识别。两端都真的跑过，才算端到端。
            st, _, dr = jcall("POST", "/api/voice/asr",
                              {"audio_b64": base64.b64encode(w1).decode()})
            check("语音-识别闭环", st == 200, f"({st}) 识别结果：{dr.get('text')!r}")
    return passed, total


def run_pipeline(lines: list[str], port: int) -> tuple[int, int]:
    """真跑一次核心流水线。返回 (通过, 总数)。"""
    total = 0
    passed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal total, passed
        total += 1
        if ok:
            passed += 1
            log(lines, f"  [PASS] {name:<20} {detail}")
        else:
            log(lines, f"  [FAIL] {name:<20} {detail}")

    st, body = call("GET", port, "/api/status", None)
    try:
        mock = json.loads(body).get("mock_mode")
    except json.JSONDecodeError:
        mock = None
    check("status-离线态", mock is True, f"mock_mode={mock!r}")
    if mock is not True:
        # 已配置真实模型时不再往下跑：真实调用会产生费用且结果不可复现。
        log(lines, "::error::实例非离线态 —— 核心流水线未跑，不得读作已验收")
        return passed, total

    ids: list[str] = []
    prints: list[str] = []
    scores: list = []
    for tag, src in (("A", SOURCE_A), ("B", SOURCE_B)):
        st, body = call("POST", port, "/api/distill",
                        {"source_prompt": src, "budget": 20000,
                         "rounds": 1, "gens": 1})
        ok = st == 200
        jid = ""
        if ok:
            try:
                jid = json.loads(body).get("job", "")
            except json.JSONDecodeError:
                ok = False
        check(f"distill-{tag}", ok and bool(jid), f"job={jid} ({st})")
        if not jid:
            return passed, total
        ids.append(jid)

        state, _d = _poll_job(lines, port, jid)
        check(f"job-{tag}-完成", state == "done", f"终态={state}")

        st, body = call("GET", port, f"/api/genome/{jid}", None)
        g = {}
        try:
            g = json.loads(body) if st == 200 else {}
        except json.JSONDecodeError:
            g = {}
        # 空壳判定：基因组里必须真的有提炼出的提示词与基因，只有键名不算。
        prompt = str(g.get("system_prompt") or "")
        genes = (g.get("persona_genes") or []) + (g.get("workflow_genes") or [])
        check(f"genome-{tag}-非空", st == 200 and len(prompt) > 50 and bool(genes),
              f"提示词 {len(prompt)} 字 / 基因 {len(genes)} 条")
        prints.append(str(g.get("source_fingerprint") or ""))

        st, body = call("GET", port, f"/api/report/{jid}", None)
        rep = {}
        try:
            rep = json.loads(body) if st == 200 else {}
        except json.JSONDecodeError:
            rep = {}
        check(f"report-{tag}-有分", st == 200 and rep.get("final_score") is not None,
              f"final_score={rep.get('final_score')!r}")
        try:
            scores.append(round(float(rep.get("final_score")), 4))
        except (TypeError, ValueError):
            scores.append(None)

    if len(prints) == 2:
        # 两次输入不同，指纹必须不同。相同则说明产物与输入无关——
        # 那正是"假功能"的形态：无论喂什么都吐同一份东西。
        check("genome-随输入变化", bool(prints[0]) and prints[0] != prints[1],
              f"{prints[0]} vs {prints[1]}")

    # 分数同样必须随输入变化，而且比指纹更关键：
    # 指纹只证明"记录了来源"，分数才是报告里最显眼的数字。
    # 曾经指纹三个都不同、分数却恒为同一个值——用户看到的
    # "蒸馏体 8.57 分"与喂进去的源材料无关，是个常量。
    # 只断言"分数不为空"抓不到这一点：常量也是非空。
    if len(scores) == 2:
        valid = [s for s in scores if s is not None]
        check("report-分数随输入变化",
              len(valid) == 2 and valid[0] != valid[1],
              f"{scores[0]} vs {scores[1]}")

    if len(ids) == 2:
        # 跨版本比对：这是界面上新补的入口，必须证明它对真实产物成立。
        st, body = call("GET", port, f"/api/compare?a={ids[0]}&b={ids[1]}", None)
        cmp_ = {}
        try:
            cmp_ = json.loads(body) if st == 200 else {}
        except json.JSONDecodeError:
            cmp_ = {}
        need_keys = ("comparable", "prev_score", "curr_score")
        check("compare-真实产物", st == 200 and all(k in cmp_ for k in need_keys),
              f"comparable={cmp_.get('comparable')!r} "
              f"prev={cmp_.get('prev_score')!r} curr={cmp_.get('curr_score')!r}")
        # 拒绝时必须给出对应理由。离线环境下两侧结论不成立属预期，
        # 但"不可比"若不带理由，用户看到的就只是一句无法行动的结论，
        # 与"比对根本没走到判定"在界面上完全一样。
        reason = str(cmp_.get("reason") or "")
        check("compare-拒绝时给出理由",
              bool(cmp_.get("comparable")) or bool(reason),
              f"comparable={cmp_.get('comparable')!r} reason={reason!r}")

    st, body = call("GET", port, "/api/runs", None)
    check("runs-含本次任务", st == 200 and all(j in body for j in ids),
          f"{len(ids)} 个任务编号")

    st, body = call("POST", port, "/api/chat", {"message": "请用一句话复述：蒸馏是什么"})
    reply = ""
    if st == 200:
        try:
            reply = str(json.loads(body).get("reply") or "")
        except json.JSONDecodeError:
            reply = ""
    check("chat-拿到回复", st == 200 and len(reply) > 0, f"{len(reply)} 字")

    return passed, total


def coverage():
    """后端路由 × 验收覆盖：返回 (全部, 未覆盖)。

    未覆盖非空即失败——验收报告写的是"全部通过"，
    而它只说明**被请求过的**那些通过了；没被请求过的必须显式登记。
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_endpoint_surface as ces  # noqa: PLC0415
    routes = ces.backend_routes()
    covered = set()
    for _n, _m, path, *_ in STEPS:
        covered.add(path.split("?")[0])
    for _n, _m, path, *_ in REACH_STEPS:
        covered.add(path.split("?")[0])
    missing = []
    for r in routes:
        base = r.rstrip("/")
        if r in covered or base in covered:
            continue
        if any(c.startswith(base + "/") for c in covered):
            continue
        if r in UNPROBED:
            continue
        missing.append(r)
    return routes, missing


def resolve(value, ctx: dict):
    """把入参里的 "$<步骤名>.<字段>" 换成前面那步返回的真实值。

    手写常量（例如固定任务编号）在数据变化后恒失败，而失败理由是"编号不
    存在"，排查方向会被带到业务代码上；引用真实返回值则始终指向当前数据。
    """
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, str) and value.startswith("$"):
        ref, _, field = value[1:].partition(".")
        obj = ctx.get(ref)
        if isinstance(obj, dict):
            return obj.get(field)
        return obj
    return value


def log(lines: list[str], msg: str) -> None:
    print(msg, flush=True)
    lines.append(msg)


def probe(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_exe(root: str) -> str:
    ext = ".exe" if sys.platform.startswith("win") else ""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith(SIDECAR) and name.endswith(ext):
                return os.path.join(dirpath, name)
    raise SystemExit(f"::error::安装目录未找到 {SIDECAR}*{ext}")


def call(method: str, port: int, path: str, payload: dict | None):
    """发一次真实请求，返回 (状态码, 响应体原文)。

    刻意不吞异常：连接失败与"连上了但回错东西"必须区分，
    合并处理会让启动问题被读成功能问题。
    """
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def verdict(lines: list[str], name: str, method: str, path: str,
            status: int, body: str) -> bool:
    """按三条口径判定单条端点，返回是否通过。"""
    if status in (404, 405, 501):
        log(lines, f"  [FAIL] {name:<20} 路由不存在 ({status}) {path}")
        return False
    # 请求被拒不等于功能可用：4xx 配一段结构化错误正文时，它是合法 JSON、
    # 也不为空，只验"路由存在 / 是 JSON / 非空"会把它判成 PASS。入参契约
    # 漂移走的正是这条路——字段改名后每个写端点都回 400，验收报告全绿。
    if not 200 <= status < 300:
        log(lines, f"  [FAIL] {name:<20} 请求被拒 ({status}) "
                   f"{body[:80]!r} {path}")
        return False
    head = body.lstrip()[:1]
    if head in ("<", ""):
        log(lines, f"  [FAIL] {name:<20} 非 JSON 或空响应 ({status}) "
                   f"首字符={head!r} {path}")
        return False
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        log(lines, f"  [FAIL] {name:<20} 响应不是合法 JSON ({status}) {path}")
        return False
    if parsed in (None, {}, "", []):
        log(lines, f"  [FAIL] {name:<20} 响应为空壳 ({status}) {path}")
        return False
    size = len(body)
    log(lines, f"  [PASS] {name:<20} {status}  {size}B  {path}")
    return True


def run_reuse(lines: list[str], port: int, ctx: dict) -> tuple:
    """第二轮复用：写第二遍 + 删掉，验累加与真删除。"""
    passed = 0
    total = 0
    for name, method, path, payload, want_in, want_not_in in REUSE_STEPS:
        st, body = call(method, port, path, resolve(payload, ctx))
        total += 1
        ok = verdict(lines, name, method, path, st, body)
        if ok and want_in is not None and want_in not in body:
            log(lines, f"  [FAIL] {name:<20} 读不回先前写入的内容 "
                       f"{want_in!r}：{body[:120]!r}")
            ok = False
        if ok and want_not_in is not None and want_not_in in body:
            # 删了却还在：删除端点静默失败时正是这个形态——请求回 200，
            # 列表里条目照旧，用户以为删掉了。
            log(lines, f"  [FAIL] {name:<20} 已删除的内容仍出现 "
                       f"{want_not_in!r}：{body[:120]!r}")
            ok = False
        if ok:
            passed += 1
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            ctx[name] = parsed
    return passed, total


def restart_and_check(lines: list[str], exe: str, env: dict, kwargs: dict,
                      port: int, timeout: int, proc, fh
                      ) -> tuple:
    """关掉再打开：用户第二天启动应用，数据必须还在。

    一次性跑完的验收碰不到这层。存储层若把数据写在临时位置、或启动
    时重建数据目录，前面所有"写入并读回"都会绿，而用户重启后面对空库。
    """
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass

    # 端口释放要等：刚 kill 就重起，新进程绑定失败退出，而旧进程已死——
    # 表现为"后端未就绪"，排查方向会被带向启动依赖。
    deadline = time.time() + 20
    while time.time() < deadline and probe(port):
        time.sleep(0.5)
    if probe(port):
        log(lines, "  [FAIL] 重启：旧进程端口未释放")
        return 0, len(RESTART_CHECKS), proc

    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=fh, stderr=subprocess.STDOUT,
                            env=env, **kwargs)
    ready = False
    for _ in range(timeout):
        time.sleep(1)
        if probe(port):
            ready = True
            break
        if proc.poll() is not None:
            break
    if not ready:
        log(lines, "  [FAIL] 重启后后端未就绪 —— 用户重开应用会打不开")
        return 0, len(RESTART_CHECKS), proc

    passed = 0
    for name, method, path, payload, want in RESTART_CHECKS:
        st, body = call(method, port, path, payload)
        if verdict(lines, name, method, path, st, body) and want in body:
            passed += 1
        else:
            log(lines, f"  [FAIL] {name:<20} 重启后数据不见了 "
                       f"{want!r}：{body[:120]!r}")
    return passed, len(RESTART_CHECKS), proc


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(
        os.environ.get("OF_PORT", "8793"))
    timeout = int(os.environ.get("OF_SMOKE_TIMEOUT", "60"))

    os.makedirs(DIAG, exist_ok=True)
    lines: list[str] = []
    log(lines, f"功能验收目标: {root}  端口 {port}")

    exe = find_exe(root)
    env = dict(os.environ)
    env["OF_PORT"] = str(port)
    kwargs: dict = {}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    # 日志不得写进安装目录：那是用户的目录，验收自己留下的文件会把
    # "卸载后还剩什么"的测量污染成看不清——残留里会混进我们自己写的
    # 东西，真正该报的运行时残留反而被淹没。
    out_path = os.path.join(tempfile.gettempdir(), "_features_out.log")
    fh = open(out_path, "wb")
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=fh, stderr=subprocess.STDOUT,
                            env=env, **kwargs)

    ready = False
    for _ in range(timeout):
        time.sleep(1)
        if probe(port):
            ready = True
            break
        if proc.poll() is not None:
            break

    if not ready:
        log(lines, "::error::安装后的后端未就绪 —— 功能验收无从进行")
        try:
            proc.kill()
        except Exception:
            pass
        fh.close()
        out = open(out_path, "rb").read().decode("utf-8", "replace")
        log(lines, f"--- 后端输出 ---\n{out[:3000]}")
        return 1
    log(lines, "后端已就绪，开始逐端点验收")

    routes, missing = coverage()
    log(lines, f"后端路由 {len(routes)} 个：功能级 {len(STEPS)} 条 / "
               f"契约级 {len(REACH_STEPS)} 条 / 未探测 {len(missing)} 个")
    if missing:
        for m in missing:
            log(lines, f"  [FAIL] 未探测且未登记：{m}")

    total = 0
    passed = 0
    ctx: dict = {}
    for name, method, path, payload, expect in STEPS:
        st, body = call(method, port, path, resolve(payload, ctx))
        total += 1
        ok = verdict(lines, name, method, path, st, body)
        if ok and expect is not None and expect not in body:
            # 接住了、回了非空 JSON，但写进去的东西读不回来——只发一次写
            # 请求看不出这个，必须读回才能区分"答了话"与"真的生效"。
            log(lines, f"  [FAIL] {name:<20} 响应里没有写入的内容 "
                       f"{expect!r}：{body[:120]!r}")
            ok = False
        if ok:
            passed += 1
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            ctx[name] = parsed

    log(lines, "--- 第二轮复用（写入第二遍 + 删除，验累加与真删除） ---")
    r_ok, r_total = run_reuse(lines, port, ctx)
    passed += r_ok
    total += r_total

    log(lines, "--- 核心流水线（离线真跑：蒸馏 → 基因组 → 报告 → 比对） ---")
    p_ok, p_total = run_pipeline(lines, port)
    passed += p_ok
    total += p_total

    log(lines, "--- 真实使用两次（技能 / 会话 / 工具：此前只发过空入参） ---")
    u_ok, u_total = run_realuse(lines, port)
    passed += u_ok
    total += u_total

    log(lines, "--- 契约级（无法跑完整功能，但必须验契约还在） ---")
    for name, method, path, payload, want_status, want_text in REACH_STEPS:
        st, body = call(method, port, path, payload)
        total += 1
        if reach_verdict(lines, name, path, st, body, want_status, want_text):
            passed += 1

    log(lines, "--- 重启后仍在（关掉应用再打开） ---")
    rs_ok, rs_total, proc = restart_and_check(
        lines, exe, env, kwargs, port, timeout, proc, fh)
    passed += rs_ok
    total += rs_total

    try:
        proc.kill()
    except Exception:
        pass
    fh.close()

    uncovered = len(missing)
    log(lines, f"=== 功能验收 {passed}/{total} 通过"
               f"（另有 {uncovered} 个端点未被任何步骤探测） ===")
    if uncovered:
        # "全部通过"只说明被请求过的那些通过了。未探测的部分若不说出来，
        # 报告会被读成"每个功能都验过"，而路由改删恰恰落在那部分里。
        log(lines, "::error::存在未探测端点 —— 不得把本次结果读作全量验收")
        total += uncovered
    try:
        with open(os.path.join(DIAG, "05-features.txt"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
