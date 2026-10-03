"""OmegaForge Chat — 对话系统：持久化 / 超长对话压缩 / Auto 模型路由。

* ConversationStore：每会话一个 JSON（OMEGAFORGE_HOME/conversations/）
* 超长对话：估算上下文超阈值时，把旧消息压缩为滚动摘要（LLM 可用则真摘要，
  离线则确定性抽取），保留最近 K 条原文 —— 长对话不爆上下文
* AutoRouter：按任务特征（长度/代码/多步指令/创作）在 fast/main 两档间路由，
  用户也可为会话固定模型 —— 对齐 MiMo Desktop "智能编排" 的最小实现
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

from ..core.atomicio import atomic_write_json, file_lock
from ..core.errors import UserError
from ..core.paths import LazyHome
from ..core.tokens import estimate_tokens

KEEP_RECENT = 12
# 压缩阈值以 **token** 为单位，不能用字符数：_est_tokens() 对中文按
# 1 字≈1 token、西文按 4 字符≈1 token 估算，拿字符数常量与它比较属于
# 单位错配——同样的阈值对中文会把防爆线推迟数倍，短上下文模型会先撑爆。
COMPACT_THRESHOLD_TOKENS = 6_000
# 旧名保留仅为兼容历史引用，语义已废弃。
COMPACT_THRESHOLD_CHARS = COMPACT_THRESHOLD_TOKENS

# 全局上下文预算（token）。"条数"与"单条字数"都是局部截断，叠加起来仍可能
# 远超模型上下文，所以必须有一层全局预算，否则请求会被上游以长度超限拒收，
# 而这类拒收容易被当成服务异常，用户只会反复重试。
# 取值依据：8k 上下文模型留足回复空间后，输入侧安全线约 6k；这里按 32k 档
# 常见下限再留余量定 24k，并让调用方在仍超预算时**点名大头**而不是静默送出。
CONTEXT_BUDGET_TOKENS = 24_000

# 单条发言上限。例如 300 万字可原样落盘且无上限；超限由用户自行拆分最可控，
# 所以这里报错而不是静默截断——当前发言是用户最在意的内容，截断会让他以为
# 系统收到了全部。
MSG_MAX_CHARS = 20_000


def _est_tokens(text: str) -> int:
    """粗估 token —— **委托给 core.tokens 的唯一实现**。

    估算口径必须全仓统一：裁剪与复核若各用一套系数，会出现"裁剪时判定
    未超预算、复核时判定超预算"——本该丢弃历史继续对话，却变成整句发不
    出去并让用户去改别的东西。
    """
    return estimate_tokens(text)


class AutoRouter:
    """规则式 auto 路由 v1：便宜模型处理轻任务，重活交给 main 档。"""

    HEAVY_WORDS = ("分析", "报告", "总结", "综述", "调研", "翻译", "写一篇",
                   "设计", "方案", "对比", "评估", "代码", "实现", "重构",
                   "analyze", "report", "design", "implement", "refactor")

    @classmethod
    def pick(cls, message: str, model_fast: str, model_main: str) -> tuple[str, str]:
        score = 0
        if len(message) > 800:
            score += 2
        if len(message) > 2000:
            score += 1
        if re.search(r"```|def |class |function |SELECT |import ", message):
            score += 2
        low = message.lower()
        score += sum(1 for w in cls.HEAVY_WORDS if w in low)
        if message.count("？") + message.count("?") >= 3:
            score += 1
        if score >= 3:
            return model_main, f"auto→main（任务特征分 {score}）"
        return model_fast, f"auto→fast（任务特征分 {score}）"


def _safe_text(raw: Any, default: str = "") -> str:
    """把任意来源的标题/偏好/正文收敛成字符串——读盘与入参共用。

    对话 JSON 是磁盘上的普通文件：较早版本本写的、手工改过的、被别的进程
    截断的都可能存在。列表页是用户看到对话的唯一入口，它一崩就是
    "对话页永久打不开"。所以这里不抛异常，只做收敛。
    """
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, str):
        s = raw.strip()
        return s or default
    if isinstance(raw, (int, float)):
        return str(raw)
    try:
        return str(raw)
    except Exception:                                   # noqa: BLE001
        return default


def _safe_float(raw: Any, default: float = 0.0) -> float:
    """时间戳收敛成 float，用于排序（脏值排到最后而不是崩掉）。"""
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v == v and abs(v) != float("inf") else default
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return default
    return default


class ConversationStore(LazyHome):
    def __init__(self, home: Optional[str] = None):
        super().__init__(home)
        # 同一会话被并发追加时后写会整体覆盖先写（见 _mutate 的说明）。
        self._lock = threading.RLock()

    @property
    def dir(self) -> str:
        d = os.path.join(self.home, "conversations")
        # 建目录必须跟着路径走，不能在实例创建时只做一次：目录可能在
        # 运行中途消失，那样之后每次写入都会失败，用户连对话页都打不开。
        os.makedirs(d, exist_ok=True)
        return d

    def _path(self, cid: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{10}", cid):
            # 必须抛 UserError：抛 ValueError 会被统一压成「请求内容有误，
            # 请检查后重试」，用户看不出是编号不对，只会盲目重试。
            raise UserError("对话编号格式不正确")
        return os.path.join(self.dir, cid + ".json")

    # ------------------------------------------------------------------
    def new(self, title: str = "新对话", model_pref: str = "auto") -> dict:
        cid = uuid.uuid4().hex[:10]
        # title 可能来自不可信的 payload（例如 {"title": null} 会把标题存成
        # 字面量 "None"，列表里真的显示一个叫 None 的对话）。空值/非串一律
        # 回落默认标题，而不是把 None 转字符串存下来。
        title = _safe_text(title, "新对话")[:40]
        model_pref = _safe_text(model_pref, "auto")[:40] or "auto"
        conv = {"id": cid, "title": title, "model_pref": model_pref,
                "created": time.time(), "updated": time.time(),
                "summary": "", "messages": []}
        self._save(conv)
        return conv

    def _save(self, conv: dict) -> None:
        conv["updated"] = time.time()
        # 目录可能在中途消失（用户清理缓存目录，或测试套件各自建删临时
        # home）。只在 __init__ 建一次的话，此后每次写入都 FileNotFoundError
        # → 500「操作失败」，而用户连"对话"页都打不开。写前确保目录在。
        os.makedirs(self.dir, exist_ok=True)
        atomic_write_json(self._path(conv["id"]), dict(conv))

    @contextlib.contextmanager
    def _mutate(self, cid: str):
        """同一会话的读-改-写临界区。

        并发写入者各自持有内存里的全量快照，不串行化就会整体覆盖彼此，
        更严重的是临时文件被同时写会产出无法解析的内容，此后这一整段
        对话都读不出来。临界区同时保证删除与追加互斥。
        """
        with self._lock:
            with file_lock(self._path(cid)):
                yield

    def get(self, cid: str) -> Optional[dict]:
        try:
            with open(self._path(cid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def list(self) -> list[dict]:
        """列出全部会话。

        单个会话文件缺字段或 messages 为 null 时不能让整个列表失败：
        列表页是清理异常会话的唯一入口，它一失败就没有别的入口可用。
        所以脏会话跳过，正常会话照常显示。
        """
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            c = self.get(fn[:-5])
            if not isinstance(c, dict):
                continue
            msgs = c.get("messages")
            if not isinstance(msgs, list):
                msgs = []                   # messages 为 null/非列表：按空处理
            out.append({"id": _safe_text(c.get("id"), fn[:-5]),
                        "title": _safe_text(c.get("title"), "未命名对话"),
                        "model_pref": _safe_text(c.get("model_pref"), "auto"),
                        "updated": _safe_float(c.get("updated")),
                        "count": len(msgs)})
        return sorted(out, key=lambda c: -c["updated"])

    def delete(self, cid: str) -> bool:
        # 删除必须进 _mutate 临界区，这一点比写入更关键：若不与追加消息
        # 互斥，删除后并发写入者会用自己内存里的全量快照把文件再写一遍，
        # 结果是"点了删除，对话又出现在列表里"且不报错——比直接失败更难
        # 察觉。并发删除同一个会话时，后到者会在存在性判定之后才真正
        # 删除，也必须靠同一把锁串行化。
        p = self._path(cid)
        lock_p = p + ".lock"
        with self._mutate(cid):
            existed = os.path.exists(p)
            if existed:
                os.remove(p)
        # 数据文件删除后，它的锁文件成了孤儿：留在用户目录里，而该会话
        # 已不存在，永远不会再被加锁——目录会随使用时长堆积这类空文件。
        # 必须在**释放锁之后**才清理：持有锁时删除会让下一个加锁者 open
        # 到新的 inode，两个进程各锁一个文件，互斥出现空窗且无人察觉。
        # 清理失败不应推翻已成功的删除，故只吞掉"本来就没有"这一情形。
        if existed:
            try:
                os.remove(lock_p)
            except FileNotFoundError:
                pass
        return existed

    def set_model_pref(self, cid: str, model_pref: str) -> dict:
        with self._mutate(cid):
            conv = self.get(cid)
            if not conv:
                raise ValueError("未找到该对话")
            conv["model_pref"] = model_pref
            self._save(conv)
        return conv

    def set_persona(self, cid: str, persona: str) -> dict:
        with self._mutate(cid):
            conv = self.get(cid)
            if not conv:
                raise ValueError("未找到该对话")
            conv["persona"] = persona
            self._save(conv)
        return conv

    # ------------------------------------------------------------------
    def add_message(self, cid: str, role: str, content: str,
                    model: str = "", interrupted: bool = False) -> dict:
        with self._mutate(cid):
            # 锁内重新读盘：get() 每次都开文件，所以这里拿到的一定是别的
            # 写入者刚落盘的最新内容，再追加才不会覆盖它们。
            conv = self.get(cid)
            if not conv:
                raise ValueError("未找到该对话")
            if not isinstance(conv.get("messages"), list):
                conv["messages"] = []       # 历史脏数据：重建而不是崩掉
            item = {"role": _safe_text(role, "user"),
                    "content": _safe_text(content),
                    "ts": time.time(),
                    "model": _safe_text(model)}
            # 流式推送到一半客户端断开时，已送出去的那些字必须标出来。
            # 不标的话它在界面上就是一条样式完好的回答，用户会当作完整
            # 答案，后续提问还会基于这半句话继续往下接。
            if interrupted:
                item["interrupted"] = True
            conv["messages"].append(item)
            self._maybe_compact(conv)
            self._save(conv)
        return conv

    def _maybe_compact(self, conv: dict) -> None:
        msgs = [m for m in conv.get("messages") or [] if isinstance(m, dict)]
        est = _est_tokens(_safe_text(conv.get("summary"))) + sum(
            _est_tokens(_safe_text(m.get("content"))) for m in msgs)
        if est < COMPACT_THRESHOLD_TOKENS or len(msgs) <= KEEP_RECENT + 2:
            return
        old, recent = msgs[:-KEEP_RECENT], msgs[-KEEP_RECENT:]
        lines = []
        for m in old:
            who = "用户" if m.get("role") == "user" else "助手"
            lines.append(f"{who}: {_safe_text(m.get('content'))[:200]}")
        prev = _safe_text(conv.get("summary"))
        conv["summary"] = (prev + "\n" + "\n".join(lines)).strip()[-4000:]
        conv["messages"] = recent
        conv["compacted"] = True

    # ------------------------------------------------------------------
    # 单条历史进入上下文的字数上限：防止一条超长消息把上下文撑爆
    HISTORY_MSG_CHARS = 1_500

    def build_context(self, conv: dict, task: str,
                      budget_tokens: int | None = None) -> tuple[str, str]:
        """返回 (system, user_payload)：摘要作系统前缀，正文带最近原文 + 当前发言。

        历史原文必须进上下文：压缩要攒到阈值才生成摘要，在触发压缩之前，
        历史对模型而言只存在于 messages 里。若不带原文，模型每轮只能看见
        当前这一句话，多轮对话没有连续性。

        去重约定：调用方（server.py）通常先把当前发言 append 进 messages 再
        调用本函数，所以最后一条 user 消息往往就是 task 本身。这里检测到尾部
        与 task 相同就剔除，避免同一句话出现两次。

        budget_tokens（全局上下文预算）
        ------------------------------
        若只有两道**局部**截断：条数 KEEP_RECENT 与单条 HISTORY_MSG_CHARS。
        最坏情况合计仍可达 22 万 token（技能正文 20 万 + 摘要 4000 +
        历史 1.8 万），远超任何模型上下文，而**没有一层做全局预算**。后果：
        上游返回 400「maximum context length」，被映射成 500「模型服务返回了
        异常响应」——把"内容太长（用户可自行缩短）"说成"服务器故障（重试无用）"，
        用户只会反复重试。

        超预算时**从最旧的历史开始丢**（保留最近对话的连续性），而不是截断
        单条内容——截断单条会让模型看到半句话，比整条丢掉更容易产生幻觉。
        丢到一条不剩仍超预算，说明大头不在历史（通常是技能正文），此时交给
        调用方报错并点名，而不是静默送出注定被拒的请求。
        """
        parts = ["You are OmegaForge assistant — rigorous, dense, no filler."]
        summary = _safe_text(conv.get("summary"))
        if summary:
            parts.append(" Earlier conversation summary (compacted):\n"
                         + summary)
        task = _safe_text(task)

        msgs = [m for m in (conv.get("messages") or [])
                if isinstance(m, dict)]
        if msgs and msgs[-1].get("role") == "user" \
                and _safe_text(msgs[-1].get("content")) == task:
            msgs = msgs[:-1]                      # 当前发言单独作为最后一轮
        msgs = msgs[-KEEP_RECENT:]

        lines = []
        for m in msgs:
            who = "用户" if _safe_text(m.get("role")) == "user" else "助手"
            body = _safe_text(m.get("content"))[:self.HISTORY_MSG_CHARS]
            if not body:
                continue
            lines.append(f"{who}: {body}")

        def _payload(lines_):
            if not lines_:
                return task
            return ("以下是本次对话此前的往返记录（仅供参考背景，不是新指令）：\n"
                    + "\n".join(lines_)
                    + "\n\n用户现在说：" + task)

        if budget_tokens:
            base = _est_tokens("\n".join(parts)) + _est_tokens(task)
            while lines and base + _est_tokens(_payload(lines)) > budget_tokens:
                lines.pop(0)                       # 从最旧开始丢
        return "\n".join(parts), _payload(lines)


def summarize_old(conv: dict, llm) -> str:
    """LLM 可用时生成真摘要；离线/失败时确定性抽取。"""
    old = conv.get("summary", "")
    try:
        r = llm.chat("Summarize the conversation so far in <=120 words, "
                     "preserving facts, decisions and open items.",
                     old[-3000:] or "(empty)", max_tokens=300)
        return r.text.strip() or old
    except Exception:                                   # noqa: BLE001
        return old
