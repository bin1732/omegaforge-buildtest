"""OmegaForge Studio server — 零依赖本地后端（纯 API）。

桌面壳（Tauri 2）通过 HTTP 调用本服务；界面由 frontend/dist 打包进壳内，
后端不托管任何 HTML —— 避免旧界面随包发布造成"两套界面并存"。

主要接口：
  GET  /api/status            -> version, budget, mock mode
  POST /api/distill           -> start distillation {source, budget, rounds, gens}
  GET  /api/jobs/<id>         -> job status + live log
  GET  /api/genome/<job>      -> distilled genome JSON
  GET  /api/report/<job>      -> distill report JSON

Threaded: distill jobs run in background workers; UI polls job status.
Run:  python -m omegaforge.server  (defaults 127.0.0.1:8787)
"""
from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .llm.client import LLMClient
from .llm.providers import ProviderManager, PRESET_MAP
from .llm.usage import UsageStore
from .core.budget import TokenBank
from .core.run import RunStore, clear_active, mark_active, phase_text
from .distill.engine import DistillEngine
from .core.tokens import estimate_tokens
from .memory.kb import KnowledgeBase
from .memory.wiki import Wiki
from .memory.tasks import Tasks
from .skills.manager import SkillManager
from .chat.store import (ConversationStore, AutoRouter,
                          CONTEXT_BUDGET_TOKENS, MSG_MAX_CHARS)
from .tools.system_tools import SystemTools, Permissions, tool_dispatch
from .tools.policy import (MODE_LABELS, POLICY_META, ApprovalRequired, PlanRequired, Policy, audit_read)
from .voice.asr import engine as voice_engine
from .voice.tts import engine as tts_engine
from .voice import fetch as voice_fetch
from .core.errors import (CODE_APPROVAL, CODE_FORBIDDEN, CODE_INVALID_INPUT,
                          CODE_NOT_FOUND, CODE_PLAN, UserError, user_error,
                          user_error_payload,
                          configure_logging, port_bind_error_text)
from .core.validate import (as_dict, as_float, as_int, as_str_list, as_text,
                            pick_first, require)

# 蒸馏数值边界以 core.limits 为唯一真源：各入口若各自解析数值，就会绕过
# 统一闸门——非法取值的任务会照常跑完并产出空结果，退出码还是 0。共用同一
# 份常量可杜绝两边漂移。
# 下界依据见 limits.py：budget<2000 必然在评测阶段抛 BudgetExceeded。
from .core.limits import (DEFAULT_BUDGET, MAX_BUDGET, MIN_BUDGET,
                          GENS_MAX, GENS_MIN, GENS_DEFAULT,
                          ROUNDS_MAX, ROUNDS_MIN, ROUNDS_DEFAULT)

# Tauri 2 架构下，界面由 src-tauri 打包的 frontend/dist 提供；
# 后端是纯 API sidecar，不再托管任何 HTML —— 避免旧界面随包发布造成"两套界面"。
JOBS: dict = {}          # 兼容旧读取路径（进程内镜像）
JOBS_LOCK = threading.Lock()
RUNS = RunStore()        # 持久化事件流：实时可读、重启可恢复

# 单个请求体的上限。与 loader 的单文件上限一致（8MB）——蒸馏源材料是最大的
# 合法输入，超过它的一律不是正常使用。必须在 read() 之前生效。
MAX_BODY_BYTES = 8 * 1024 * 1024

# 共享个人数据服务（进程级单例，数据目录 OMEGAFORGE_HOME）
KB = KnowledgeBase()
WIKI = Wiki(kb=KB)
TASKS = Tasks()
SKILLS = SkillManager()
USAGE = UsageStore()
PROVIDERS = ProviderManager()
CONVS = ConversationStore()
SYSTOOLS = SystemTools()
PERMS = Permissions()
POLICY = Policy()


def _estimate_tokens(text: str) -> int:
    """粗估 token 数 —— **委托给 core.tokens 的唯一实现**。

    原实现用 len(text)//3，对中文严重低估——中文 1 字常占 1~2 个 token，
    一条 300 字的中文回复会被算成 100，用量页因此显示远低于真实消耗。

    委托给 core.tokens 的唯一实现，不在本地另算一套：两套系数会让"裁剪"
    与"复核"互相打架——同一份内容一处判定未超预算、另一处判定超预算，
    本该丢弃历史继续对话，却变成整句发不出去。唯一真源后两边永远一致。
    """
    return estimate_tokens(text)


def _run_job(run, source: str, budget: int, rounds: int, gens: int,
             out_dir: str, task: str = "", eval_set: list | None = None,
             baseline_prompt: str = "") -> None:
    """执行蒸馏，并把全过程实时写入 Run 事件流。

    关键修复：原有写法把日志攒在内存 list 里，任务结束时才一次性写入
    JOBS —— 运行期间前端轮询到的 log 恒为空，表现为"点了没反应、
    进度条不动"。这里改为每条日志立即落盘，前端可增量拉取。
    """
    jid = run.id
    mark_active(jid)          # 本进程正在跑它 —— 重启后本登记天然消失
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "status": "running", "out_dir": out_dir}

    try:
        # 准备阶段必须在错误处理之内：它同样会失败（客户端构造、记账器
        # 初始化、写第一条日志都碰磁盘与配置）。这段必须纳入错误处理：
        # 一旦出错而线程直接死亡，登记表不清、状态卡在"运行中"、事件流没有
        # 终态，界面表现为进度条永远不动且没有任何报错。
        class LogCapture:
            @staticmethod
            def write(msg: str) -> None:      # noqa: ANN001
                # 每条日志即时落盘（Run.emit 内部 append + fsync）
                run.log(str(msg).rstrip())

        def on_phase(phase: str) -> None:
            # 消息由后端拼好后落盘，前端的标签映射管不到它：这里不转中文，
            # 运行详情的事件流就会显示"进入阶段 ingest"这类中英夹杂的句子。
            run.phase_to(phase, message=f"进入阶段：{phase_text(phase)}")

        llm = LLMClient()
        bank = TokenBank(budget, on_charge=lambda ph, mo, tk, p, c:
                         USAGE.record(ph, mo, tk, p, c))
        if task:
            run.log(f"评测任务：{task}")
        engine = DistillEngine(llm, bank, arena_rounds=rounds,
                               max_generations=gens, verbose=False,
                               on_phase=on_phase, task=task, eval_set=eval_set)
        engine._log = LogCapture.write        # capture pipeline logs
        genome, report = engine.distill(source, output_dir=out_dir,
                                        task=task,
                                        baseline_prompt=baseline_prompt)
        run.set_meta(
            verdict=report.verdict,
            final_score=report.final_score,
            baseline_score=report.baseline_score,
            generation=report.generation,
            tokens_spent=bank.total_spent,
            budget=budget,
            # 对照组可信度：决定"更强"这个结论能不能成立
            baseline_kind=report.baseline_kind,
            baseline_comparable=report.baseline_comparable,
            baseline_note=report.baseline_note,
            claim_valid=report.claim_valid,
            # 评测集指纹：跨版本比对的唯一凭据。
            # 缺了它，/api/runs 列表里看不出哪两次是同一套题——
            # 用户会把不同题的分数相减当成进步。
            eval_set_fingerprint=report.eval_set_fingerprint,
            eval_set_cases=report.eval_set_cases,
        )
        run.succeed(verdict=report.verdict, final_score=report.final_score,
                    baseline_score=report.baseline_score,
                    claim_valid=report.claim_valid)
        with JOBS_LOCK:
            JOBS[jid].update({"status": "done", "verdict": report.verdict,
                              "final_score": report.final_score})
    except Exception as e:                  # noqa: BLE001
        # 绝不把异常原文写进事件流：这里的内容会直接渲染到界面日志区，
        # 而 str(e) 可能含异常类名、接口地址、本地路径甚至 URL 里的密钥。
        # 完整 traceback 由 user_error 写入内部日志（.omegaforge/logs/errors.log）。
        msg = user_error(e, "distill.run_job")
        run.fail(msg)
        with JOBS_LOCK:
            JOBS[jid].update({"status": "error", "error": msg})
    finally:
        try:
            # 兜底终态：任何原因让线程结束却没有终态事件时都要补上，
            # 否则读取方把它当成还在跑，界面无限等待。
            run.ensure_terminal("任务未跑完，执行线程已结束")
            with JOBS_LOCK:
                cur = JOBS.get(jid)
                if cur and cur.get("status") in ("running", "queued"):
                    cur.update({"status": "error",
                                "error": "任务未跑完，执行线程已结束"})
        finally:
            clear_active(jid)



# 只读端点：实现在 do_POST 上，但前端用 GET 调用（例如 404，功能等于没有）
_POST_READ_ALIASES = {"/api/voice/status", "/api/voice/models/progress"}

#: 模型类型的用户说法 -> 内部标识。
#:
#: 提示里若写"asr 或 tts"，用户看到的是两个不认识的英文缩写；改成中文
#: 说法却仍只收英文标识，则"照提示填中文"会失败——与提示自相矛盾。
#: 因此两端都收，内部统一存标识。
VOICE_KIND_ALIASES = {
    "asr": "asr", "tts": "tts",
    "语音识别": "asr", "语音合成": "tts",
    "识别": "asr", "合成": "tts",
}


def _voice_kind(raw: str) -> str:
    return VOICE_KIND_ALIASES.get(str(raw or "").strip().lower(), "")


def _public_voice_status(st: dict) -> dict:
    """语音状态转译：例如 status() 会把模型目录的**绝对路径**返回给前端，
    等于把本机目录结构（含用户名）暴露在界面上。这里只保留用户判断
    「能不能用、为什么不能用」所需的信息，路径一个字都不外传。
    """
    return {"model": st.get("model", ""),
            "ready": bool(st.get("ready")),
            "lib_installed": bool(st.get("lib_installed")),
            "files_missing": list(st.get("files_missing") or []),
            "error": st.get("error") or ""}


def _prepare_chat(payload: dict, msg: str) -> dict:
    """流式与非流式共用的对话准备逻辑（唯一真源）。

    为什么必须抽出来
    ----------------
    若两条路径各自复制准备逻辑，任何只加在一边的处理都会静默漂移。
    例如同一对话、同一句提问，非流式可能携带人设而流式整条丢弃。
    根因不是"忘了写一行"，而是**两条路径各自复制了一遍准备逻辑**，于是
    任何只加在一边的东西都会静默漂移：

      * 人设       —— 只加在非流式。前端若只用 SSE（流式），人设功能整体失效，
                      且流式请求里带的 persona 连存都不存（例如 conv.persona=None）。
      * 用量记账   —— 只加在非流式。流式前后 USAGE.summary() 完全不变，
                      等于所有流式对话的消耗不入账，用量页系统性少记。
      * token 口径 —— 流式只报 completion 估算（例如 5），非流式报提示词与补全之和
                      （例如 120），前端同一个字段两个数。
      * 陈旧会话ID —— 非流式报"未找到该对话"，流式却静默新建一个空对话
                      （返回全新 id），用户以为还在原对话，历史凭空消失。

    抽成单一函数后，这些差异在结构上就不可能再出现：两条路径拿到的
    是同一个 dict。以后新增任何"对话前必做的事"，只改这一处。

    返回值：{cid, conv, cfg, model, why, system, task}
    """
    # 发言上限必须**先于建会话**校验：反了就会在请求被拒之后留下一个空会话。
    # 发一条 20 万字的消息 -> 400 正确拒绝，但会话列表
    # 凭空多出一条，标题还是这条超长消息的前 24 字 —— 失败的写入留下了
    # 用户看得见、却无从删除来源的脏数据。
    if len(msg) > MSG_MAX_CHARS:
        raise UserError(
            f"这条消息过长（{len(msg):,} 字符，上限 {MSG_MAX_CHARS:,}），"
            "请拆分后再发送")
    cid = str(payload.get("conversation_id") or "")
    if not cid:
        conv = CONVS.new(title=msg[:24])
        cid = conv["id"]
    else:
        conv = CONVS.get(cid)
        if not conv:
            # 两条路径都必须报错：静默新建会让用户以为历史还在，实际已被换掉。
            raise UserError("未找到该对话，可能已被清理")
    cid = str(conv.get("id") or cid)     # 防御：cid 永远跟随实际会话
    pref = str(payload.get("model") or conv.get("model_pref", "auto"))
    conv = CONVS.add_message(cid, "user", msg)
    _, cfg = PROVIDERS.active()
    md = cfg.get("models") or {}
    fast = md.get("fast") or "gpt-4o-mini"
    main = md.get("main") or "gpt-4o-mini"
    if pref == "auto":
        chosen, why = AutoRouter.pick(msg, fast, main)
    else:
        chosen, why = pref, "手动指定"
    if payload.get("persona"):
        conv = CONVS.set_persona(cid, str(payload["persona"]))
    persona = str(payload.get("persona")
                  or conv.get("persona", "")).strip()
    # 先算人设块，再把"剩余预算"交给 build_context 去裁剪历史——顺序不能反：
    # 反了就会先按满额预算装历史，再把技能正文叠上去，裁剪等于没做。
    p_block = ""
    if persona:
        try:
            # 技能正文默认带分隔标记（invoke 的 wrap=True），因为它进的是 system
            # 这个最高优先级位置，而技能文件是用户可编辑的不可信输入。
            p_block = SKILLS.invoke(persona, msg) + "\n\n"
        except FileNotFoundError:
            pass                       # 人设未安装：静默降级，不打断对话
    p_tok = _estimate_tokens(p_block)
    system, task = CONVS.build_context(
        conv, msg, budget_tokens=max(1_000, CONTEXT_BUDGET_TOKENS - p_tok))
    system = p_block + system
    total = _estimate_tokens(system) + _estimate_tokens(task)
    if total > CONTEXT_BUDGET_TOKENS:
        # 历史已被裁到一条不剩仍超预算 → 大头不在历史。点名而不是静默送出
        # 注定被上游拒收的请求（拒收会被映射成 500「模型服务返回了异常
        # 响应」，用户只会反复重试）。
        who = f"人设/技能「{persona}」的正文" if persona else "当前内容"
        raise UserError(
            f"{who}过长（约 {total:,} token，预算 {CONTEXT_BUDGET_TOKENS:,}），"
            f"请精简{who}或缩短当前消息后重试")
    return {"cid": cid, "conv": conv, "cfg": cfg, "model": chosen,
            "why": why, "system": system, "task": task}


# 允许跨域的来源（按 host 判定，端口不限）。
#
# 仅绑定回环地址并不足以保护本机服务：请求是浏览器在用户这一侧发起的，
# 用户浏览的任意网页都能向本机服务发起读取并拿到返回内容，知识库、
# 对话记录、待办、任务列表与审计日志因此全部可读（GET 不触发预检）。
# 所以来源必须按白名单判定，预检与实际请求都要校验。
#
# 允许来源只放这四类：本机 dev（vite 5173）、Tauri 生产壳
# （http://tauri.localhost；macOS 为 tauri://localhost）、回环地址。
_ALLOWED_ORIGIN_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "[::1]", "tauri.localhost"})


def _allowed_origin_hosts() -> frozenset:
    """白名单 + 环境变量追加。

    生产环境实际发来的来源地址无法逐一预判。留一个显式开关：
    出现漏网来源时不用改代码重启即可放行。
    默认仍是最小的那四个，不会因这个开关变宽。
    """
    extra = os.getenv("OMEGAFORGE_ALLOWED_ORIGINS", "")
    if not extra.strip():
        return _ALLOWED_ORIGIN_HOSTS
    return _ALLOWED_ORIGIN_HOSTS | frozenset(
        h.strip().lower() for h in extra.split(",") if h.strip())


def _origin_host(origin: str) -> str:
    """从 Origin 取出 host（小写、去端口）。取不到则返回空串。"""
    o = (origin or "").strip().lower()
    if not o:
        return ""
    if "://" in o:
        rest = o.split("://", 1)[1]
    else:                                  # 例如 "null"
        return ""
    return rest.split("/", 1)[0].rsplit(":", 1)[0] if rest else ""


class Handler(BaseHTTPRequestHandler):
    def _cors_header(self) -> str | None:
        """按 Origin 决定 CORS 响应头：来源可信则回显，否则不发。

        不发 ACAO 时浏览器会拦截读取——跨域读取因此不成立。
        无 Origin（curl / 我们自己的测试 / 非浏览器客户端）时返回 "*"：
        这类调用方不受同源策略约束，发不发都不影响，保持兼容。
        """
        origin = self.headers.get("Origin", "")
        if not origin:
            return "*"
        return origin if _origin_host(origin) in _allowed_origin_hosts() else None

    def _origin_allowed(self) -> bool:
        """来源是否可信。

        读靠"不发 ACAO"拦；但**写**不能只靠它——Content-Type: text/plain
        属于 CORS 简单请求，不触发预检，恶意页面照样能 POST 一段纯文本
        过来，而我们按 JSON 解析就会照做。所以来源不可信时直接拒绝。
        """
        origin = self.headers.get("Origin", "")
        return (not origin) or _origin_host(origin) in _allowed_origin_hosts()

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        acao = self._cors_header()
        if acao:
            self.send_header("Access-Control-Allow-Origin", acao)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_OPTIONS(self) -> None:    # noqa: N802
        """CORS 预检 —— 缺了它，界面上所有写操作全部失效。

        若服务未实现 OPTIONS 预检，基础请求处理器会一律返回 501。

        而前端（frontend/src/lib/api.ts:74）POST 时带
        ``Content-Type: application/json`` —— 这不是 CORS 安全listed
        类型，浏览器因此**必须先发 OPTIONS 预检**。预检拿不到 2xx，
        真实请求就不会被发出。触发场景覆盖全部真实使用环境：

          * Tauri 生产壳：Win/Linux 来源 http://tauri.localhost，
            macOS 为 tauri://localhost
          * vite 开发态：http://localhost:5173（见 src-tauri/tauri.conf.json）

        也就是说：**界面上每一个 POST 都被静默拦下**，表现为点了没反应。
        前端缺少编译验证时，这个问题不会被真实执行暴露。

        预检同样做来源校验：恶意站点的预检直接 403，不给任何放行头。
        """
        if not self._origin_allowed():
            self._json({"error": "不允许的访问来源",
                        "code": CODE_FORBIDDEN}, 403)
            return
        self.send_response(204)
        acao = self._cors_header()
        if acao:
            self.send_header("Access-Control-Allow-Origin", acao)
            self.send_header("Vary", "Origin")
            # 只声明真正支持的动词与请求头，不写 "*"
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    def do_GET(self) -> None:   # noqa: N802
        if not self._origin_allowed():
            self._json({"error": "不允许的访问来源", "code": CODE_FORBIDDEN}, 403)
            return
        # 为什么 do_GET 必须有顶层兜底：
        # 读取请求同样要有兜底：一旦某个分支抛出未捕获异常（例如知识库里存
        # 了类型非法的标签），异常会穿透处理器，服务端线程直接崩、连接被
        # 关闭且不返回任何响应。客户端只能收到网络错误，前端无法区分"后端
        # 挂了"还是"这次请求有问题"，而且响应体里连错误码都没有。
        # 这里补上与写入请求完全一致的兜底，保证任何异常都变成一条 JSON。
        try:
            self._do_get()
        except UserError as e:
            self._json(user_error_payload(e), 400)
        except ValueError as e:
            self._json(user_error_payload(e, "api.get"), 400)
        except FileNotFoundError as e:
            self._json(user_error_payload(e, "api.get"), 404)
        except ApprovalRequired as e:
            # 能力已开，但本次动作超出当前权限级别的免确认范围。
            # 这不是错误——是一次交互：返回凭证，用户批准后带上重放即可。
            self._json({"error": e.reason, "code": CODE_APPROVAL,
                        "need_approval": True, "approval": e.approval}, 409)
        except PlanRequired as e:
            self._json({"plan": e.plan, "code": CODE_PLAN,
                        "message": "计划模式：以下操作待你确认后再执行"}, 200)
        except PermissionError as e:
            d = user_error_payload(e, "api.get")
            d["need_permission"] = True
            self._json(d, 403)
        except Exception as e:                    # noqa: BLE001
            self._json(user_error_payload(e, "api.get"), 500)

    def _do_get(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # 根路径只做存活探测，不托管界面（界面在 Tauri 壳内）
            self._json({"service": "omegaforge", "ok": True})
        elif path == "/api/status":
            llm = LLMClient()
            # 预算用真实数据：上限取 DEFAULT_BUDGET，已用取全局用量台账。
            spent = int(USAGE.summary().get("total", 0) or 0)
            self._json({"version": "0.1.0", "mock_mode": llm.mock_mode,
                        "models": {"main": llm.model_main,
                                   "fast": llm.model_fast,
                                   "judge": llm.model_judge},
                        "jobs": len(JOBS),
                        "budget": {"limit": DEFAULT_BUDGET,
                                   "spent": spent,
                                   "remaining": max(0, DEFAULT_BUDGET - spent)}})
        elif path == "/api/usage":
            self._json(USAGE.summary())
        elif path == "/api/tools/permissions":
            self._json({"permissions": PERMS.load()})
        elif path == "/api/tools/policy":
            meta = POLICY_META()
            meta["current"] = POLICY.mode()
            self._json(meta)
        elif path == "/api/tools/audit":
            # 审计日志必须有读取端点：只写不读等于没写。
            q = parse_qs(urlparse(self.path).query)
            self._json({"entries": audit_read(
                limit=as_int({"v": (q.get("limit") or ["50"])[0]}, "v", 50,
                             minimum=1, maximum=500, label="条数"),
                tool=(q.get("tool") or [None])[0],
                verdict=(q.get("verdict") or [None])[0])})
        elif path == "/api/providers":
            active, cfg = PROVIDERS.active()
            self._json({"presets": PROVIDERS.status_all(),
                        "active": active,
                        "current": {"base_url": cfg.get("base_url", ""),
                                    "has_key": bool(cfg.get("api_key")),
                                    "models": cfg.get("models", {})}})
        elif path == "/api/providers/models":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            preset = PRESET_MAP.get(name)
            if not preset:
                self._json({"error": "未找到该模型供应商", "code": CODE_NOT_FOUND}, 404)
                return
            catalog = list(preset.get("model_catalog") or [])
            if preset["local"]:
                probe = PROVIDERS.probe(preset["base_url"])
                for mid in probe.get("models") or []:
                    if mid and mid not in catalog:
                        catalog.append(mid)
            self._json({"name": name, "catalog": catalog,
                        "online": (PROVIDERS.probe(preset["base_url"])
                                   ["online"] if preset["local"] else None)})
        elif path == "/api/personas":
            personas = [m for m in SKILLS.list()
                        if m.get("type") == "persona"]
            cats = {}
            for m in personas:
                cats.setdefault(m.get("category", "通用"), []).append(
                    # 这里一律用带默认值的取值，不写硬下标：条目上缺少某个
                    # 字段时，硬下标会把"上游少给一个字段"直接放大成整页报错，
                    # 而这里只是展示用途，取不到就该留空。
                    {"slug": m.get("_dir") or m.get("name", ""),
                     "name": m.get("name", ""),
                     "description": m.get("description", "")})
            self._json({"categories": cats, "total": len(personas)})
        elif path == "/api/conversations":
            self._json({"conversations": CONVS.list()})
        elif path.startswith("/api/conversations/"):
            cid = path.rsplit("/", 1)[-1]
            conv = CONVS.get(cid)
            if not conv:
                # 与其他端点保持一致：资源不存在走 404，
                # 否则前端按状态码分支处理时会把错误体当成正常数据。
                self._json({"error": "未找到该对话，可能已被清理",
                            "code": CODE_NOT_FOUND}, 404)
                return
            self._json(conv)


        elif path == "/api/kb/search":
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            # 不再二次 unquote：parse_qs 已完成 URL 解码，
            # 再解一次会把用户字面输入的 "%20"/"%2B" 吃掉
            # （例如：搜"折扣%20大"被改成"折扣 大"，5 个用例 4 个失真）。
            self._json({"results": KB.search(q, limit=8)})
        elif path == "/api/kb/list":
            self._json({"docs": KB.all()[:50], "total": KB.count()})
        elif path == "/api/wiki/list":
            self._json({"pages": WIKI.pages()})
        elif path == "/api/wiki/page":
            qs = parse_qs(urlparse(self.path).query)
            # 已知情况：前端 wikiPage() 发 ?name=<slug>，这里只读 slug
            # → 用户在列表里点开词条，恒定 404「未找到该词条」，而该词条在
            # /api/wiki/list 里明明列着。列表与详情对不上，且重试永远不会
            # 成功。与 kb.search 一脉：两个名字都认，优先更明确的 slug。
            slug = (qs.get("slug") or qs.get("name") or [""])[0]
            if "q" in qs:
                self._json({"results": WIKI.search(qs["q"][0])})
                return
            page = WIKI.get(slug)
            if not page:
                self._json({"error": "未找到该词条", "code": CODE_NOT_FOUND}, 404)
                return
            self._json(page)
        elif path == "/api/tasks/list":
            qs = parse_qs(urlparse(self.path).query)
            scope = (qs.get("scope") or ["pending"])[0]
            self._json({"items": TASKS.list(scope), "stats": TASKS.stats()})
        elif path == "/api/skills/list":
            self._json({"skills": SKILLS.list()})
        elif path == "/api/memory/recall":
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            self._json({"memories": KB.recall(q, limit=8)})
        elif path == "/api/runs":
            self._json({"runs": RUNS.list()})
        elif path == "/api/compare":
            # 跨版本比对：本版比上一版本强了多少、依据哪套题。
            # 不做路由只做 CLI 的话，界面上永远看不到——而界面才是主入口。
            qs = parse_qs(urlparse(self.path).query)
            a = (qs.get("a") or [""])[0].strip()
            b = (qs.get("b") or [""])[0].strip()
            if not a or not b:
                self._json({"error": "请提供要比对的两个任务编号"}, 400)
                return
            ra, rb = RUNS.get(a), RUNS.get(b)
            if ra is None or rb is None:
                self._json({"error": "未找到该任务，可能已完成清理"}, 404)
                return
            pair = {}
            for tag, run in (("prev", ra), ("curr", rb)):
                fp = os.path.join(run.dir, "report.json")
                if not os.path.exists(fp):
                    self._json({"error": "该任务的报告尚未生成，请等待任务完成"}, 404)
                    return
                try:
                    with open(fp, encoding="utf-8") as f:
                        pair[tag] = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    user_error(e, context="compare read report")
                    self._json({"error": "读取报告失败，请重新生成"}, 500)
                    return
            self._json(DistillEngine.compare_reports(pair["prev"], pair["curr"]))
        elif path.startswith(("/api/genome/", "/api/report/")):
            # 基因组与报告是蒸馏核心产物，前端需要能独立获取，
            # 而不必为了拿报告去拉整个任务事件流。
            kind, jid = path.rsplit("/", 2)[-2], path.rsplit("/", 1)[-1]
            fname = "genome.json" if kind == "genome" else "report.json"
            run = RUNS.get(jid)
            if run is None:
                self._json({"error": "未找到该任务，可能已完成清理"}, 404)
                return
            fp = os.path.join(run.dir, fname)
            if not os.path.exists(fp):
                # 产品文案：产物未就绪是正常中间态，不是错误
                label = "基因组" if fname == "genome.json" else "报告"
                self._json({"error": f"{label}尚未生成，请等待任务完成后再查看"}, 404)
                return
            try:
                with open(fp, encoding="utf-8") as f:
                    self._json(json.load(f))
            except (json.JSONDecodeError, OSError) as e:
                # 不回显路径与异常原文；完整信息写入内部日志
                user_error(e, context=f"read {fname}")
                self._json({"error": "读取产物失败，请重新生成"}, 500)
        elif path.startswith("/api/jobs/"):
            jid = path.rsplit("/", 1)[-1]
            run = RUNS.get(jid)          # 从磁盘恢复（进程重启也不丢）
            if run is None:
                self._json({"error": "未找到该任务，可能已完成清理"}, 404)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                since = int((qs.get("since") or [0])[0])
            except (TypeError, ValueError):
                since = 0
            st = run.state()
            evs = run.events(since=since)
            # 状态约定：内部用精确语义（succeeded/failed/cancelled），
            # 但 status 字段映射回旧前端约定的 done/error ——
            # 直接改用新词会让既有前端永远等不到完成。
            # 新前端请读 outcome 字段。
            st["outcome"] = st.get("status")
            st["status"] = {
                "succeeded": "done", "failed": "error",
                "cancelled": "cancelled",
                "interrupted": "interrupted",
            }.get(st.get("status"), st.get("status"))
            # 增量日志：前端拿 since 轮询即可持续追加
            st["log"] = [e.get("message", "")
                         for e in evs if e.get("type") == "log"]
            st["events"] = evs
            # 兼容旧前端字段（verdict / final_score / genome / report …）
            meta = st.get("meta") or {}
            for k in ("verdict", "final_score", "baseline_score",
                      "generation", "tokens_spent", "budget",
                      "baseline_kind", "baseline_comparable",
                      "baseline_note", "claim_valid"):
                if k in meta:
                    st[k] = meta[k]
            for fname, key in (("genome.json", "genome"),
                               ("report.json", "report")):
                fp = os.path.join(run.dir, fname)
                if os.path.exists(fp) and key not in st:
                    try:
                        with open(fp, encoding="utf-8") as f:
                            st[key] = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        pass
            self._json(st)
        elif path in _POST_READ_ALIASES:
            # 例如：GET /api/voice/status 返回 404「接口不存在」——
            # 该路由只挂在 do_POST 上，而前端 voiceStatus() 用 GET 拉。
            # 语音设置页因此拿不到任何数据。只读端点应同时接受 GET。
            self.do_POST()
            return
        else:
            self._json({"error": "接口不存在", "code": CODE_NOT_FOUND}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._json({"error": "不允许的访问来源", "code": CODE_FORBIDDEN}, 403)
            return
        path = urlparse(self.path).path
        raw_len = self.headers.get("Content-Length", "0")
        # Content-Length 是**客户端提供的**，不是我们算出来的。原实现直接
        # int() + read(n)，三种后果，全都不是"报错"而是更难诊断的事故：
        #   "abc" / "12.5"  -> ValueError 逃出 try（只兜了 JSONDecodeError），
        #                      连接被直接断开，客户端收到的是网络错误而非 HTTP 400
        #   "-1"            -> read(-1) 一直读到 EOF，请求永久挂起
        #   "999999999999"  -> 预分配约 1TB，例如 MemoryError，进程可被单请求打爆
        # 头部必须在读体之前校验并封顶，否则"先分配再判断"本身就已中招。
        try:
            length = int(str(raw_len).strip())
        except (TypeError, ValueError):
            self._json({"error": "请求头部格式有误", "code": CODE_INVALID_INPUT}, 400)
            return
        if length < 0:
            self._json({"error": "请求头部格式有误", "code": CODE_INVALID_INPUT}, 400)
            return
        if length > MAX_BODY_BYTES:
            self._json({"error": f"请求内容过大（上限 {MAX_BODY_BYTES // (1024 * 1024)}MB），"
                                 "请拆分后重试", "code": CODE_INVALID_INPUT}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "请求格式有误，无法解析", "code": CODE_INVALID_INPUT}, 400)
            return

        def need(*keys, **_labels):
            # 例如：原有写法只提示「请填写完整的必填项」，界面上有多个输入框时
            # 用户无法定位到底缺哪个。这里按中文标签逐个点名。
            require(payload, keys, _labels)

        try:
            if path == "/api/chat/stream":
                # 校验必须早于 SSE 响应头：头一旦发出，状态码与
                # Content-Type 就无法再改，此时再回 JSON 会产生畸形响应。
                msg = str(payload.get("message", "")).strip()
                if not msg:
                    self._json({"error": "请输入消息内容",
                                "code": CODE_INVALID_INPUT}, 400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                acao = self._cors_header()
                if acao:
                    self.send_header("Access-Control-Allow-Origin", acao)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                try:
                    prep = _prepare_chat(payload, msg)
                except UserError as ue:
                    # 头已发出，改不了状态码：只能推 error + done，
                    # 保证前端 loading 一定会结束（不推 done 会永远转圈）。
                    ev0 = lambda o: (self.wfile.write(  # noqa: E731
                        ("data: " + json.dumps(o, ensure_ascii=False) + "\n\n").encode()),
                        self.wfile.flush())
                    ev0({"type": "error", "error": str(ue)})
                    ev0({"type": "done", "error": str(ue)})
                    return
                cid, conv = prep["cid"], prep["conv"]
                cfg, chosen, why = prep["cfg"], prep["model"], prep["why"]
                system, task = prep["system"], prep["task"]
                # 客户端中途断开（关窗口、切走页面）是**正常行为**，不是故障。
                # 但两件事必须做对，否则用户会被误导：
                #   1. 不能把它记进错误日志——不然真实故障会被这类噪声淹没；
                #   2. 已推送出去的内容必须在落库时标成"被中断"，否则界面上
                #      它看起来就是一条正常回答，用户以为这就是完整答案，
                #      后续对话还会基于这半句话继续。
                class _Disconnected(Exception):
                    pass

                def ev(obj):
                    try:
                        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
                        self.wfile.flush()
                    except OSError as exc:
                        # 只认两种类型是不够的：Windows 上客户端关掉连接，写入
                        # 抛的是 ConnectionAbortedError（WSAECONNABORTED），
                        # 不在旧名单里。漏掉它的后果是半截回复被当成完整回答
                        # 落库——界面上不显示"已中断"，用户以为模型就说这么多。
                        if _is_disconnect(exc):
                            raise _Disconnected()
                        raise
                interrupted = False
                ev({"type": "start", "conversation_id": cid, "model": chosen, "routing": why})
                reply = ""
                real_usage = None      # 兜底成功时上游给的真实用量（流式无 usage）
                try:
                    req = urllib.request.Request(
                        (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
                        data=json.dumps({"model": chosen, "stream": True,
                                         "max_tokens": 2048,
                                         "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": task}]}).encode(),
                        headers={"Content-Type": "application/json",
                                 "Authorization": "Bearer " + cfg.get("api_key", "")})
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        for raw in resp:
                            line = raw.decode("utf-8", errors="replace").strip()
                            if not line.startswith("data:"):
                                continue
                            chunk = line[5:].strip()
                            if chunk == "[DONE]":
                                break
                            try:
                                delta = json.loads(chunk)["choices"][0]["delta"].get("content") or ""
                            except Exception:
                                continue
                            if delta:
                                reply += delta
                                ev({"type": "delta", "text": delta})
                except _Disconnected:
                    # 客户端已断开：不再尝试兜底（推不出去），也不记错误日志。
                    interrupted = True
                except Exception as e:
                    # 不立刻推 error：下面还有非流式兜底。
                    # 例如：无外网时流式必失败，若此时就推 error 事件，
                    # 界面会先闪一条错误、兜底成功后又冒出正文 ——
                    # 用户以为"报错了又好了"。错误只在兜底也拿不到回复时才发。
                    # 此处调用仅为把完整异常写入内部日志，供排查。
                    user_error(e, "chat_stream")
                if interrupted:
                    # 客户端已不在了，兜底与结束事件都没有意义。
                    # 但仍要落库：界面上已经显示过的文字不能凭空消失。
                    if reply:
                        try:
                            CONVS.add_message(cid, "assistant", reply,
                                              model=chosen, interrupted=True)
                        except Exception:                      # noqa: BLE001
                            pass
                        try:
                            USAGE.record("chat", chosen,
                                         _estimate_tokens(system)
                                         + _estimate_tokens(task)
                                         + _estimate_tokens(reply),
                                         _estimate_tokens(system)
                                         + _estimate_tokens(task),
                                         _estimate_tokens(reply))
                        except Exception:                      # noqa: BLE001
                            pass
                    return
                if not reply:
                    try:
                        rr = LLMClient().configure(
                            cfg.get("base_url") or "https://api.openai.com/v1",
                            cfg.get("api_key", ""),
                            {"main": chosen, "fast": chosen}).chat(
                            system, task, model=chosen, max_tokens=2048)
                        reply = rr.text
                        ev({"type": "delta", "text": reply})
                        # 兜底走的是非流式接口，上游会返回真实 usage。
                        # 若此处丢弃真实用量、改用估算，"流式失败→兜底成功"
                        # 这一类对话将全部按估算记账，用量页系统性失真。
                        real_p = getattr(rr, "prompt_tokens", 0) or 0
                        real_c = getattr(rr, "completion_tokens", 0) or 0
                        if isinstance(real_p, int) and isinstance(real_c, int) \
                                and real_p >= 0 and real_c > 0:
                            real_usage = (real_p, real_c)
                        else:
                            real_usage = None
                    except Exception as e2:        # noqa: BLE001
                        # 失败时必须同时推结束信号：
                        # 而 SSE 头已发出、状态码改不了，前端收不到结束信号，
                        # loading 永不结束（用户看着红字一直转圈）。
                        # 与 _prepare_chat 失败那条路径的处理保持一致。
                        err_txt = user_error(e2, "chat_stream_fallback")
                        ev({"type": "error", "error": err_txt})
                        ev({"type": "done", "error": err_txt,
                            "finish_reason": "error"})
                        return
                conv = CONVS.add_message(cid, "assistant", reply, model=chosen)
                # 流式不返回用量，只能估算——但必须记，否则所有流式对话的
                # 消耗都会漏记。
                # 口径与非流式对齐：提示词 + completion，而不是只报 completion。
                if real_usage is not None:
                    p_tok, c_tok = real_usage
                    estimated = False
                else:
                    p_tok = _estimate_tokens(system) + _estimate_tokens(task)
                    c_tok = _estimate_tokens(reply)
                    estimated = True
                try:
                    USAGE.record("chat", chosen, p_tok + c_tok, p_tok, c_tok)
                except Exception:                              # noqa: BLE001
                    pass          # 记账失败不能影响已经拿到回复的对话
                try:
                    ev({"type": "done", "conversation_id": cid,
                        "tokens": p_tok + c_tok,
                        "estimated": estimated})
                except _Disconnected:
                    # 内容已完整落库，只是结束事件没送出去——客户端既然已
                    # 断开，界面不会再转圈，无需额外处理，也不记错误日志。
                    pass
                return
            if path == "/api/distill":
                self._json(self._start_distill(payload))
            elif path == "/api/chat":
                cid = str(payload.get("conversation_id") or "")
                msg = str(payload.get("message", "")).strip()
                if not msg:
                    raise UserError("请输入消息内容")
                prep = _prepare_chat(payload, msg)
                cid, conv = prep["cid"], prep["conv"]
                cfg, chosen, why = prep["cfg"], prep["model"], prep["why"]
                system, task = prep["system"], prep["task"]
                t0 = time.time()
                r = LLMClient().configure(
                    cfg.get("base_url", "https://api.openai.com/v1"),
                    cfg.get("api_key", ""),
                    {"main": chosen, "fast": chosen}).chat(
                    system, task, model=chosen, max_tokens=2048)
                USAGE.record("chat", r.model, r.prompt_tokens + r.completion_tokens,
                             r.prompt_tokens, r.completion_tokens)
                conv = CONVS.add_message(cid, "assistant", r.text,
                                         model=r.model)
                self._json({"conversation_id": cid,
                            "reply": r.text,
                            "model_used": r.model,
                            "routing": why,
                            "latency_ms": int((time.time() - t0) * 1000),
                            "tokens": r.prompt_tokens + r.completion_tokens,
                            "mock": r.mock})
            elif path == "/api/conversations/new":
                # 例如：{"title": null} 会被 str() 转成字面量 "None" 存下来，
                # 列表里真的显示一个叫 None 的对话。空值回落默认标题。
                self._json(CONVS.new(
                    str(payload.get("title") or "新对话"),
                    str(payload.get("model") or "auto")))
            elif path == "/api/conversations/delete":
                # 例如：前端按 /api/chat 的约定发 conversation_id，这里却只读 id。
                # 结果 delete("") 触发"对话编号格式不正确"，被映射成
                # 400「请求内容有误」——用户在界面上点删除，得到一句
                # 指向不了任何输入框的话，且重试永远不会成功。
                # 两个名字都认，优先更明确的 conversation_id。
                cid = pick_first(payload, ("conversation_id", "id"))
                if not cid:
                    # 缺编号时要先说缺什么：直接往下走会落到编号格式校验，
                    # 而 ValueError 被统一压成「请求内容有误」，用户在界面上
                    # 点删除得到一句指向不了任何输入框的话。
                    raise UserError("请填写：对话")
                self._json({"deleted": CONVS.delete(cid)})
            elif path == "/api/conversations/model":
                # 同上：前端发 conversation_id，这里却要求 id，恒定 400。
                cid = pick_first(payload, ("conversation_id", "id"))
                if not cid:
                    raise UserError("请填写：对话")
                need("model", model="模型")
                conv = CONVS.set_model_pref(cid, str(payload["model"]))
                self._json({"id": conv["id"],
                            "model_pref": conv["model_pref"]})
            elif path == "/api/voice/status":
                self._json({"asr": _public_voice_status(voice_engine().status()),
                            "tts": _public_voice_status(tts_engine().status())})
            elif path == "/api/voice/tts":
                need("text", text="要朗读的文字")
                # 例如：speed=[1] → TypeError→500「操作失败」，属误报故障。
                self._json(tts_engine().synthesize(
                    as_text(payload, "text", label="要朗读的文字"),
                    speed=as_float(payload, "speed", 1.0, minimum=0.25,
                                   maximum=4.0, label="语速")))
            elif path == "/api/voice/asr":
                if not payload.get("audio_b64"):
                    raise UserError("未提供音频数据")
                self._json(voice_engine().recognize_wav_b64(
                    str(payload["audio_b64"])))
            elif path == "/api/voice/models/progress":
                # 这个端点同时接受 GET（前端轮询）与 POST。GET 走
                # _POST_READ_ALIASES 转发到 do_POST，请求体是空的——只看
                # payload 会恒定拿到空值并报"请填写模型类型"，而前端明明传了
                # kind，界面表现为"永远查不到进度"。因此回退到查询参数。
                # GET 经别名转发时请求体为空，payload 可能不是字典：直接
                # `.get` 会抛 AttributeError，被兜底转成 500「操作失败」——
                # 真实原因（没有请求体）被吞掉，且重试永远不会成功。
                kind = ""
                payload_kind = ""
                if isinstance(payload, dict):
                    payload_kind = str(payload.get("kind") or "").strip()
                query_kind = (parse_qs(urlparse(self.path).query)
                              .get("kind") or [""])[0].strip()
                kind = _voice_kind(payload_kind) or _voice_kind(query_kind)
                if not kind:
                    raise UserError("请填写：模型类型（语音识别或语音合成）")
                self._json(voice_fetch.read_progress(kind))
            elif path == "/api/voice/models/install":
                kind = _voice_kind(payload.get("kind"))
                if not kind:
                    raise UserError("请填写：模型类型（语音识别或语音合成）")
                self._json(voice_fetch.start(kind))
            elif path == "/api/tools/permissions":
                # 例如：请求体是 "abc" / 123 时 PERMS.save 抛
                # AttributeError → 500「操作失败」。用户只是传错了格式，
                # 却被报成服务器故障，且重试永远不会成功。
                if not isinstance(payload, dict):
                    raise UserError("请求格式有误，请提交一组能力开关")
                self._json({"permissions": PERMS.save(payload)})
            elif path == "/api/tools/policy":
                if not isinstance(payload, dict):
                    raise UserError("请求格式有误，请提交权限级别")
                m = str(payload.get("mode", "")).strip()
                self._json({"mode": POLICY.set_mode(m),
                            "label": MODE_LABELS.get(m, "")})
            elif path == "/api/tools/exec":
                need("name", name="工具名称")
                self._json(tool_dispatch(
                    str(payload["name"]), payload.get("arguments") or {},
                    approval=(str(payload["approval"])
                              if payload.get("approval") else None),
                    idem_key=(str(payload["idem_key"])
                              if payload.get("idem_key") else None)))
            elif path == "/api/kb/search":
                # 例如：前端 kbSearch() 用 POST 提交查询词，而该路由只挂在
                # do_GET 上 —— 一调用就是 404「接口不存在」，搜索框形同虚设。
                # 联调已知情况：字段名也对不上——前端发 query，这里读 q，
                # 于是查询词恒为空串，库里明明有"折扣策略"，搜"折扣"返回 []。
                # 与 conversations 一脉：两个名字都认，优先更明确的 q。
                self._json({"results": KB.search(
                    pick_first(payload, ("q", "query")) or "", limit=8)})
            elif path == "/api/memory/recall":
                # 同上：前端 recallMemory() 用 POST，路由只在 do_GET 上。
                # 同次联调例如：同样发 query / 读 q，检索恒返回空。
                self._json({"memories": KB.recall(
                    pick_first(payload, ("q", "query")) or "", limit=8)})
            elif path == "/api/kb/add":
                need("title", "text", title="标题", text="内容")
                # 例如：tags=123 能 200 落盘，此后 search 的 " ".join(int)
                # 抛 TypeError 直击 do_GET 顶层，检索永久不可用。
                self._json({"id": KB.add(
                    as_text(payload, "title", label="标题"),
                    as_text(payload, "text", label="内容"),
                    type=as_text(payload, "type", "note", label="类型"),
                    tags=as_str_list(payload, "tags", label="标签"))})
            elif path == "/api/kb/delete":
                # 存储层早有 delete()，但没有 HTTP 出口：用户在界面上加错
                # 一条就再也删不掉，只能手工改数据文件——而数据文件在用户
                # 目录里，用户无从下手。能力写好了却拿不到，等于没有。
                did = pick_first(payload, ("doc_id", "id"))
                if not did:
                    raise UserError("请填写：知识编号")
                self._json({"deleted": KB.delete(str(did))})
            elif path == "/api/wiki/save":
                need("slug", "title", slug="标识", title="标题")
                self._json({"slug": WIKI.save(
                    str(payload["slug"]), str(payload["title"]),
                    str(payload.get("body", "")))})
            elif path == "/api/tasks/add":
                need("text", text="任务内容")
                # 例如：priority=[1] 会抛 TypeError→500「操作失败」，
                # 用户只是填错了格式，却被报成服务器故障。
                self._json(TASKS.add(
                    as_text(payload, "text", label="任务内容"),
                    as_int(payload, "priority", 2, minimum=1, maximum=5,
                           label="优先级")))
            elif path == "/api/wiki/delete":
                # 同 kb/delete：Wiki.delete() 已存在，缺的只是出口。
                slug = pick_first(payload, ("slug", "id"))
                if not slug:
                    raise UserError("请填写：词条标识")
                self._json({"deleted": WIKI.delete(str(slug))})
            elif path == "/api/tasks/done":
                # 联调已知情况：前端 taskDone() 发 {id}，这里只读 task_id
                # → 恒定 400「请填写：任务编号」。用户点的是按钮，没有输入框
                # 可填，重试永远不会成功。与 conversations 一脉：两个名字都认。
                tid = pick_first(payload, ("task_id", "id"))
                if not tid:
                    raise UserError("请填写：任务编号")
                self._json({"completed": TASKS.complete(str(tid))})
            elif path == "/api/tasks/delete":
                # 同前：Tasks.delete() 已在，界面上没有删除入口。
                tid = pick_first(payload, ("task_id", "id"))
                if not tid:
                    raise UserError("请填写：任务编号")
                self._json({"deleted": TASKS.delete(str(tid))})
            elif path == "/api/skills/install":
                need("path", path="技能包路径")
                # 安装失败的原因必须让用户看见：技能名含中文、目录不存在、
                # SKILL.md 缺字段等，ValueError 都带着具体原因。若在此处
                # 吞成"请求内容有误"，用户拿到的是一句无法行动的提示，
                # 而能力本身其实是可用的——与功能坏了无法区分。
                try:
                    self._json(SKILLS.install(str(payload["path"])))
                except ValueError as e:
                    raise UserError(str(e))
            elif path == "/api/skills/invoke":
                need("name", name="技能名称")
                self._json({"prompt": SKILLS.invoke(
                    str(payload["name"]), str(payload.get("context", "")))})
            elif path == "/api/memory/remember":
                need("fact", fact="要记住的内容")
                self._json({"id": KB.remember(
                    as_text(payload, "fact", label="要记住的内容"),
                    tags=as_str_list(payload, "tags", label="标签"))})
            elif path == "/api/providers/apply":
                need("name", name="模型供应商")
                name = str(payload["name"])
                preset = PRESET_MAP.get(name)
                if not preset:
                    raise UserError("未找到该模型供应商，请检查名称")
                # 例如：models="x" 或 models=[1] 都触发 AttributeError→500，
                # 而这只是一次普通的填写错误。
                models = as_dict(payload, "models", label="模型配置")
                cfg = {"base_url": str(payload.get("base_url")
                                        or preset["base_url"]),
                       "api_key": str(payload.get("api_key", "")),
                       "models": {"main": as_text(models, "main",
                                                  preset["models"]["main"],
                                                  label="主模型"),
                                  "fast": as_text(models, "fast",
                                                  preset["models"]["fast"],
                                                  label="快速模型"),
                                  "judge": as_text(models, "judge",
                                                   preset["models"]["judge"],
                                                   label="评审模型")}}
                PROVIDERS.save(name, cfg)
                self._json({"applied": name, "config": {
                    "base_url": cfg["base_url"],
                    "has_key": bool(cfg["api_key"]),
                    "models": cfg["models"]}})
            elif path == "/api/providers/test":
                name = str(payload.get("name", "custom"))
                preset = PRESET_MAP.get(name,
                                        {"base_url": "", "models": {
                                            "main": "", "fast": "", "judge": ""}})
                base = str(payload.get("base_url") or preset["base_url"])
                key = str(payload.get("api_key", ""))
                # 例如：models=[1,2] → 'list' object has no attribute 'get'
                # → 500「操作失败」。这里用安全取值，非法类型给明确提示。
                model = as_text(as_dict(payload, "models", label="模型配置"),
                                "main", preset["models"]["main"],
                                label="主模型")
                if not base or not model:
                    raise UserError("接口地址与模型名称为必填项")
                probe = PROVIDERS.probe(base, key)
                t0 = time.time()
                chat_ok, chat_err, sample = False, "", ""
                try:
                    c = LLMClient().configure(base, key, {"main": model})
                    r = c.chat("Reply with one word: OK", "ping",
                               model=model, max_tokens=8)
                    chat_ok, sample = True, (r.text or "")[:40]
                except Exception as e:              # noqa: BLE001
                    chat_err = user_error(e, "provider_probe")
                self._json({"endpoint": probe, "chat_ok": chat_ok,
                            "chat_error": chat_err, "sample": sample,
                            "latency_ms": int((time.time() - t0) * 1000),
                            "model": model})
            else:
                self._json({"error": "接口不存在", "code": CODE_NOT_FOUND}, 404)
        except UserError as e:
            self._json(user_error_payload(e), 400)
        except ValueError as e:
            self._json(user_error_payload(e, "api"), 400)
        except FileNotFoundError as e:
            self._json(user_error_payload(e, "api"), 404)
        except ApprovalRequired as e:
            # 能力已开，但本次动作超出当前权限级别的免确认范围。
            # 这不是错误——是一次交互：返回凭证，用户批准后带上重放即可。
            self._json({"error": e.reason, "code": CODE_APPROVAL,
                        "need_approval": True, "approval": e.approval}, 409)
        except PlanRequired as e:
            self._json({"plan": e.plan, "code": CODE_PLAN,
                        "message": "计划模式：以下操作待你确认后再执行"}, 200)
        except PermissionError as e:
            d = user_error_payload(e, "api")
            d["need_permission"] = True
            self._json(d, 403)
        except Exception as e:                # noqa: BLE001
            self._json(user_error_payload(e, "api"), 500)

    @staticmethod
    def _start_distill(payload: dict) -> dict:
        # 字段名兼容（例如：前端 ForgePage.tsx 发的是 source_提示词，
        # 后端只读 source —— 用户填完点"开始蒸馏"必然 400「请先粘贴…」，
        # 主功能在界面上从来没能用过）。两个名字都认，优先新名。
        source = pick_first(payload, ("source_prompt", "source")).strip()
        if not source:
            raise UserError("请先粘贴源 Agent 的系统提示词")
        # 用户在界面上填的评测任务：必须真的被读取并使用，否则等于白填。
        task = as_text(payload, "task", "", label="评测任务", max_len=2000)
        # 数值校验（例如：budget=0/-5、rounds=0/-3、gens=0 全部 200 放行，
        # 任务随即空转并标记"完成"，用户拿到空产物却查不出原因）。
        # 下界 2000 来自例如：mock 模式下 1500 必抛 BudgetExceeded，2000 可跑完。
        budget = as_int(payload, "budget", DEFAULT_BUDGET,
                        minimum=MIN_BUDGET, maximum=MAX_BUDGET, label="预算")
        rounds = as_int(payload, "rounds", ROUNDS_DEFAULT,
                        minimum=ROUNDS_MIN, maximum=ROUNDS_MAX,
                        label="评测轮次")
        gens = as_int(payload, "gens", GENS_DEFAULT, minimum=GENS_MIN, maximum=GENS_MAX,
                      label="进化代数")
        # 冻结评测集（可选）：HTTP 侧只接受**内联用例**，不接受文件路径——
        # 接受路径等于让调用方指定本机任意文件去读，是信息泄露口子。
        # 跨次复用走产物里的 eval_set.json：前端把上次的内容回传即可。
        # 对照组 提示词：源材料无可提取 提示词 时的唯一补救手段。
        # 只接受内联文本，不接受文件路径——与 eval_set 同一原则。
        baseline_prompt = as_text(payload, "baseline_prompt", "",
                                  label="对照组提示词", max_len=20000)
        raw_eval = payload.get("eval_set")
        eval_set = (DistillEngine._normalize_eval_set(raw_eval)
                    if isinstance(raw_eval, (list, dict)) else None)
        # 持久化 Run：id 即目录名，产物与事件流同目录
        run = RUNS.create(source, budget=budget, rounds=rounds, gens=gens,
                          task=task)
        jid, out_dir = run.id, run.dir
        with JOBS_LOCK:
            JOBS[jid] = {"id": jid, "status": "queued", "out_dir": out_dir}
        threading.Thread(
            target=_run_job,
            args=(run, source, budget, rounds, gens, out_dir, task, eval_set,
                  baseline_prompt),
            daemon=True).start()
        return {"job": jid, "run": jid}



def _is_disconnect(exc: BaseException) -> bool:
    """写入失败是否只因对端已经断开。

    断开在三个平台上的长相不同：Linux 多为 BrokenPipeError /
    ConnectionResetError，Windows 多为 ConnectionAbortedError，也可能直接
    是带 WSA 编号的 OSError。只按类型名认会漏掉其中一两种，而漏掉的后果
    不是报错，是**半截回复被当成完整回答**落库——界面上不显示"已中断"，
    用户以为模型就说了这么多。
    """
    if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError)):
        return True
    return getattr(exc, "errno", None) in _LOST_CONNECTION_ERRNOS


_LOST_CONNECTION_ERRNOS = frozenset(
    e for e in (
        *[getattr(errno, n, None) for n in (
            "EPIPE", "ECONNRESET", "ECONNABORTED", "ESHUTDOWN", "ENOTCONN")],
        *[getattr(errno, n, None) for n in (
            "WSAECONNRESET", "WSAECONNABORTED", "WSAESHUTDOWN",
            "WSAENOTCONN", "WSAEHOSTUNREACH")],
    ) if e is not None
)

def _sibling_exe_baseline() -> set[str]:
    """取与本程序同目录的其他可执行文件，作为"是否已卸载"的基线。

    本程序运行时自身文件被占用，卸载器删不掉它，故不能拿自身是否被删
    当信号；同目录里没有在运行的文件则会被正常删掉。启动时刻记录基线，
    之后基线中任一文件消失即说明安装已被卸载。

    卸载器自身须排除：静默卸载时它不会删除自己，把它算进基线的话"全部
    消失"永远不成立，这条判定一次都不会生效。
    """
    me = os.path.abspath(sys.executable)
    here = os.path.dirname(me)
    out: set[str] = set()
    try:
        for name in os.listdir(here):
            if not name.lower().endswith(".exe"):
                continue
            if name.lower().startswith("uninstall"):
                continue
            p = os.path.join(here, name)
            if os.path.abspath(p) == me or not os.path.isfile(p):
                continue
            out.add(p)
    except OSError:
        return set()
    return out


def _start_uninstall_watch(httpd: ThreadingHTTPServer,
                           interval: float = 0.5) -> threading.Thread | None:
    """卸载后本进程必须自行退出。

    卸载器不终止后台进程时，用户以为卸掉了，进程却仍在占端口、仍在写
    数据。本程序无法感知卸载动作，只能靠"同目录其他可执行文件已全部
    消失"这一可见信号判定。

    基线为空时不启用：安装目录里本就没有其他可执行文件的话，这个信号
    无法工作，启用只会一启动就自杀。宁可不监控，也不可误退。
    """
    if not getattr(sys, "frozen", False):
        return None
    baseline = _sibling_exe_baseline()
    if not baseline:
        return None

    def watch() -> None:
        while True:
            time.sleep(interval)
            # 任一消失即判定已卸载：安装目录内的可执行文件不会被别的场景
            # 删掉。要求"全部消失"的话，只要有一个不会被卸载删除的文件
            # 留在目录里，这条判定就永远不成立。
            if all(os.path.exists(p) for p in baseline):
                continue
            print("[sidecar] 检测到安装已被卸载，退出")
            try:
                httpd.shutdown()
            except Exception:
                pass
            # 轮询间隔须短于卸载器删除文件的耗时：卸载器删不掉被占用的
            # 文件时会静默跳过，之后不再重试——本程序晚一步退出，这些文件
            # 就永久留在用户磁盘上，而卸载界面显示的是卸载成功。
            return

    t = threading.Thread(target=watch, name="uninstall-watch", daemon=True)
    t.start()
    return t


#: 后端可监听的端口候选（按顺序试探）。界面按同一组端口做发现，两边必须
#: 一致，否则换端口后界面仍去连旧端口，症状会表现成"服务没启动"。
PORT_CANDIDATES: tuple[int, ...] = (8787, 8788, 8789, 8790, 8791, 8792)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """启动本机服务。

    "已就绪"的声明必须晚于绑定成功：端口被占用时绑定会抛 OSError，
    若先声明后绑定，使用者会照着一个并没有在监听的地址反复排查。

    端口被占用时向后试探候选端口，而不是直接退出：占用 8787 的常见来源
    是上一次没退干净的本程序残留进程，此时界面只会显示"无法连接到本机
    服务"，用户既退不掉占用程序也换不了端口，等于应用彻底不可用。换端口
    后由界面按候选端口探测发现，两端都不再依赖单一端口。

    仅默认端口享受这一退让；显式指定的端口被占用时必须报错退出。
    """
    configure_logging()
    httpd = None
    last_exc: OSError | None = None
    # 只有默认端口才退到候选列表：命令行显式 --port 必须被严格尊重，否则
    # 使用者要求换端口却拿到另一个端口，"换个端口启动"这条处置建议就无从
    # 执行，且报错点名的端口与实际监听的端口不一致。
    candidates = PORT_CANDIDATES if port == PORT_CANDIDATES[0] else (port,)
    for candidate in candidates:
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
        except OSError as exc:
            last_exc = exc
            continue
        if candidate != port:
            # 必须 flush：标准输出被外壳以管道接管时是块缓冲的，而端口
            # 退让恰恰是"服务明明在跑、界面却连不上"时唯一能解释现状的
            # 一行。等进程退出才出现，排查已经结束了。
            print(f"端口 {port} 被占用，已改用 {candidate}", flush=True)
        port = candidate
        break
    if httpd is None:
        # 全部候选都被占用才算失败：此时确实起不来，必须给出可执行的处置
        # 建议，而不是笼统说"服务未启动"。
        raise SystemExit(
            port_bind_error_text(port, last_exc) if last_exc
            else f"候选端口 {list(PORT_CANDIDATES)} 均被占用，请退出占用程序后重试"
        )
    print(f"Ω OmegaForge Studio → http://{host}:{port}", flush=True)
    _start_uninstall_watch(httpd)
    httpd.serve_forever()


def _serve_main(argv=None) -> None:
    """服务启动入口。

    监听地址与端口必须是可配置项。若只能改代码才能换端口，那么端口被
    占用时给出的处置建议就无从执行——使用者既退不掉占用程序，也换不了
    端口。help 与报错口径沿用命令行层，避免同一产品两套说法。
    """
    from .cli import _CliParser

    parser = _CliParser(prog="python -m omegaforge.server",
                        description="启动本机服务，供界面与桌面外壳连接。")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787,
                        help="监听端口，默认 8787")
    args = parser.parse_args(argv)
    if not 1 <= int(args.port) <= 65535:
        parser.error("--port 需要填写 1 到 65535 之间的整数")
    serve(host=args.host, port=int(args.port))


if __name__ == "__main__":
    _serve_main()
