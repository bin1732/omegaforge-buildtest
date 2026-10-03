"""Run — 一次蒸馏执行的持久化事件流。

设计要点：
  1. 旧的 JOBS 是内存 dict，且 log 只在任务结束时一次性写入
     → 运行期轮询恒为空，前端"进度 0%、点了没反应"。
     本模块改为：每产生一个事件即 append 落盘，轮询可读到增量。
  2. 旧的 JOBS 进程重启即丢失
     → 本模块用 JSONL 持久化到磁盘，可随时 read_back 重建。

事件流而非状态快照：追加写天然无锁竞争，且天然支持
  - 实时轮询（带 cursor 增量拉取）
  - 断点续读（进程重启后从磁盘恢复）
  - 事后复盘（完整时间线）
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from .atomicio import atomic_write_json
from .paths import resolve_home as _resolve_home

# 终态：进入后不再变化
#: 终态：进入后不再变化。
#: interrupted 表示"任务没跑完，但已经不可能再跑下去了"——承载它的进程
#: 已退出（关闭、崩溃、重启），而事件流里没有终态事件。若不给它一个终态，
#: 读取方只能看到一个停在半路的任务，界面会一直轮询、永远等不到结果。
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})

# 阶段 -> 进度百分比（用于前端进度条，0-100）
PHASE_PROGRESS: dict[str, int] = {
    "queued": 0,
    "ingest": 5,
    "extract": 15,
    "compress": 28,
    "synthesize": 42,
    "gen_eval": 55,
    # 每个会上报的阶段都必须在表里有对应项：缺一项则该阶段的进度取不到
    # 值、回退到 0，进度条会倒退，用户会以为任务出错而重跑。
    "freeze_eval": 60,
    "arena": 72,
    "evolve": 86,
    "ablation": 90,
    "finalize": 96,
    "succeeded": 100,
    "failed": 100,
    "cancelled": 100,
    "interrupted": 100,
}

#: 阶段标识 → 中文说法。
#:
#: 为什么后端也需要这一份：事件消息是**后端拼好再落盘**的（
#: ``phase_to(phase, message=f"进入阶段：{phase_text(phase)}")``），
#: 它不经过前端的任何标签映射。少了这一份，运行详情的事件流就会显示
#: 中英夹杂的句子，而前端 format.ts 里的同名映射救不了它——
#: 那份只作用于前端自己渲染的字段。
#:
#: 取值取自 PHASE_PROGRESS 的真实键，不是按名字猜的语义；
#: 未收录的标识原样返回，不做猜测性翻译。
PHASE_TEXT: dict[str, str] = {
    "queued": "排队中",
    "ingest": "读取中",
    "extract": "提取中",
    "compress": "压缩中",
    "synthesize": "合成中",
    "gen_eval": "生成评测",
    "freeze_eval": "冻结评测集",
    "arena": "竞技场对比",
    "evolve": "进化中",
    "ablation": "基因消融",
    "finalize": "收尾中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def phase_text(phase: str) -> str:
    """阶段标识的中文说法；未收录的标识原样返回。"""
    return PHASE_TEXT.get(phase, phase)


#: 本进程内"正在运行"的任务 id。
#: 判定孤儿的唯一依据：一个任务若不在本集合里，它就不可能还在跑——
#: 承载它的线程随进程一起消失，事件流不可能再有新内容。进程重启后本集合
#: 天然为空，于是所有未跑到终态的历史任务都会被正确识别为中断。
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def mark_active(run_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(run_id)


def clear_active(run_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(run_id)


def is_active(run_id: str) -> bool:
    with _ACTIVE_LOCK:
        return run_id in _ACTIVE


def _now() -> float:
    return time.time()


def _safe_seq(raw) -> int | None:
    """seq 收敛成非负整数；不可解析返回 None（由调用方决定丢弃）。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _safe_ts(raw) -> float:
    """ts 收敛成浮点；不可解析返回 0.0（时间戳缺失不影响事件本身）。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and v not in (float("inf"), float("-inf")) else 0.0


@dataclass
class Event:
    """事件流中的一条记录。"""

    seq: int
    ts: float
    type: str                      # log|phase|progress|artifact|error|done
    phase: str = ""                # ingest|extract|...|done
    message: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type,
                "phase": self.phase, "message": self.message,
                "data": self.data}

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        # 与 events() 同源：脏 seq/ts 收敛而非抛异常。from_dict 目前只被
        # stream() 调用（无外部调用点），但它是公开 API，一旦接上就是同一个坑。
        return cls(seq=_safe_seq(d.get("seq")) or 0,
                   ts=_safe_ts(d.get("ts")),
                   type=str(d.get("type", "log")),
                   phase=str(d.get("phase", "")),
                   message=str(d.get("message", "")),
                   data=d.get("data") or {})


class Run:
    """一次执行的句柄。线程安全：写盘加锁，读无锁。"""

    def __init__(self, run_id: str, directory: str):
        self.id = run_id
        self.dir = directory
        self.path = os.path.join(directory, "events.jsonl")
        self.meta_path = os.path.join(directory, "run.json")
        self._lock = threading.Lock()
        self._seq = 0
        self._status = "queued"
        self._phase = "queued"
        self._meta: dict[str, Any] = {}
        os.makedirs(directory, exist_ok=True)

    # ------------------------------------------------------------------ 元信息
    @property
    def status(self) -> str:
        return self._status

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def progress(self) -> int:
        # 终态优先：进入终态就代表这件事结束了，进度条必须到顶。
        # 否则停在半路的任务会显示 15%，而它其实再也不会前进。
        if self._status in TERMINAL:
            return 100
        return PHASE_PROGRESS.get(self._phase, 0)

    def set_meta(self, **kw: Any) -> None:
        self._meta.update(kw)
        self._flush_meta()

    def _flush_meta(self) -> None:
        # seq 一并落盘：列表页要显示进度，不该为此回放整个事件流。
        # 例如：330 个 run 时 list() 耗时 3.5 秒，且随 run 只增不减线性恶化
        # —— 每个 run 都要 get() 一次，而 get() 会逐行读完事件流来算 seq。
        payload = {"id": self.id, "status": self._status, "phase": self._phase,
                   "progress": self.progress, "seq": self._seq,
                   "updated_ts": _now(), **self._meta}
        atomic_write_json(self.meta_path, payload)

    # ------------------------------------------------------------------ 写入
    def emit(self, type_: str, message: str = "", phase: str | None = None,
             **data: Any) -> Event:
        """追加一个事件并立即落盘（这是修复"运行期日志为空"的关键）。"""
        with self._lock:
            self._seq += 1
            if phase:
                self._phase = phase
                if phase in TERMINAL:
                    self._status = phase
            ev = Event(seq=self._seq, ts=_now(), type=type_,
                       phase=self._phase, message=message, data=data)
            line = json.dumps(ev.to_dict(), ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._flush_meta()
        return ev

    def log(self, message: str, phase: str | None = None) -> Event:
        return self.emit("log", message, phase=phase)

    def phase_to(self, phase: str, message: str = "") -> Event:
        return self.emit("phase", message or phase, phase=phase)

    def artifact(self, name: str, **data: Any) -> Event:
        return self.emit("artifact", name, name=name, **data)

    def fail(self, error: str) -> Event:
        return self.emit("error", error, phase="failed", error=error)

    def succeed(self, **data: Any) -> Event:
        return self.emit("done", "succeeded", phase="succeeded", **data)

    def cancel(self) -> Event:
        return self.emit("done", "cancelled", phase="cancelled")

    def interrupt(self, reason: str = "") -> Event:
        """标记为中断：事件流停在中途，而承载它的进程已不在。

        与 failed 的区别：failed 是任务自己跑到了失败结论；
        interrupted 是任务根本没机会跑完，进度停在半路。
        """
        return self.emit("done", "interrupted", phase="interrupted",
                         error=reason or "任务未跑完，所在进程已退出")

    def ensure_terminal(self, reason: str = "") -> bool:
        """没有终态时补一个中断终态；已有终态则原样不动。

        为什么需要它：执行线程有可能在错误处理之外结束——准备阶段出错、
        被系统打断、或写终态这一步本身失败。此时事件流停在半路，而任务
        已不可能再有新内容，读取方看到的就是一个永远进行中的任务：界面
        进度条不动，也不给任何报错。补终态让它当场可判定，而不是要等到
        下一次进程重启才被识别。
        """
        if self._status in TERMINAL:
            return False
        self.interrupt(reason)
        return True

    # ------------------------------------------------------------------ 读取
    def events(self, since: int = 0) -> list[dict]:
        """读取 seq > since 的事件。文件不存在返回空列表。"""
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue          # 容忍半行（进程被杀时可能截断）
                # seq 脏值（"abc"/null）曾经让 int() 抛异常，而这里只兜了
                # JSONDecodeError——于是一条坏行让整个事件流不可用，
                # 界面表现为任务详情打不开。单条脏记录跳过，绝不连累全局。
                seq = _safe_seq(d.get("seq"))
                if seq is None:
                    continue
                if seq > since:
                    out.append(d)
        return out

    def stream(self) -> Iterator[Event]:
        for d in self.events():
            yield Event.from_dict(d)

    def log_lines(self) -> list[str]:
        return [e["message"] for e in self.events() if e.get("type") == "log"]

    def state(self) -> dict:
        """供 API 返回的快照（含 progress，前端进度条可直接用）。"""
        return {"id": self.id, "status": self._status, "phase": self._phase,
                "progress": self.progress,
                "seq": self._seq,
                "meta": dict(self._meta)}


def _is_hex_id(name: str) -> bool:
    """与 RunStore.get 同一套校验：不接受非十六进制 id，防路径穿越。"""
    return bool(name) and all(c in "0123456789abcdef" for c in name)


def _replay_seq(path: str) -> int:
    """只读事件流的 seq 最大值。历史 run 缺 seq 缓存时才用。"""
    seq = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        seq = max(seq, int(json.loads(line).get("seq", 0)))
                    except (ValueError, TypeError):
                        continue
    except (OSError, json.JSONDecodeError):
        return 0
    return seq


def _progress_of(phase: str) -> int:
    return PHASE_PROGRESS.get(phase, 0)


class RunStore:
    """Run 的注册表 + 磁盘目录管理。"""

    def __init__(self, home: str | None = None):
        # 惰性：只记住"是否显式指定"，路径每次访问时解析。
        # 详见 omegaforge/core/paths.py 的说明——在 __init__ 期固化会让
        # 模块级单例 RUNS 在 fork/改环境变量后仍读旧目录。
        self._home = home or None
        self._explicit = home

    @property
    def home(self) -> str:
        return _resolve_home(self._explicit)

    @home.setter
    def home(self, value: str | None) -> None:
        # 兼容既有测试里的 `srv.RUNS.home = tmp` 写法。
        # 赋值即视为"显式指定"，此后不再跟随环境变量。
        self._explicit = value or None
        self._home = value or None

    @property
    def runs_dir(self) -> str:
        return os.path.join(self.home, "runs")

    def create(self, source: str, **meta: Any) -> Run:
        run_id = uuid.uuid4().hex[:12]
        d = os.path.join(self.runs_dir, run_id)
        r = Run(run_id, d)
        r.set_meta(source=source[:400], created_ts=_now(), **meta)
        # 消息同样落盘进事件流，用阶段标识本身会让第一条事件显示成英文。
        r.emit("phase", phase_text("queued"), phase="queued")
        return r

    def _read_meta(self, run_id: str) -> dict | None:
        """只读 run.json，供列表页使用。读不到就当该条不存在。"""
        fp = os.path.join(self.runs_dir, run_id, "run.json")
        try:
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def get(self, run_id: str) -> Run | None:
        """从磁盘恢复一个 Run（含已写入的事件与元信息）。

        这解决了原有写法"进程重启任务全丢"的问题。
        """
        # 只接受十六进制 id，防止路径穿越
        if not run_id or not all(c in "0123456789abcdef" for c in run_id):
            return None
        d = os.path.join(self.runs_dir, run_id)
        if not os.path.isdir(d):
            return None
        r = Run(run_id, d)
        # 回放元信息
        if os.path.exists(r.meta_path):
            try:
                with open(r.meta_path, encoding="utf-8") as f:
                    m = json.load(f)
                r._status = m.get("status", "queued")
                r._phase = m.get("phase", "queued")
                r._meta = {k: v for k, v in m.items()
                           if k not in ("id", "status", "phase", "progress",
                                        "updated_ts")}
            except (json.JSONDecodeError, OSError):
                pass
        # seq：优先用 run.json 里的缓存值（O(1)）。
        # 只有历史 run（缓存引入前写的）才退回回放事件流。
        # 例如：这一步是 /api/runs 的全部开销来源——330 个 run 时每次列表
        # 都要读完 330 份事件流，耗时 3.5 秒且随时间线性恶化。
        if not r._seq:
            try:
                with open(r.path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            r._seq = max(r._seq,
                                         int(json.loads(line).get("seq", 0)))
            except (OSError, json.JSONDecodeError):
                pass
        # 孤儿归位：状态停在半路，而本进程并没有在跑它 —— 承载它的线程
        # 已随上一次进程退出而消失，事件流不可能再有新内容。此时必须给
        # 出终态，否则读取方看到的是一个永远进行中的任务。
        if r._status not in TERMINAL and not is_active(r.id):
            # phase 保留：它记录的是"实际停在哪一步"，是事实，改成
            # interrupted 会让用户看不出任务跑到哪儿断的。
            r._status = "interrupted"
            r._meta.setdefault("interrupted", True)
            r._meta["note"] = "任务未跑完，所在进程已退出"
        return r

    def list(self, limit: int = 50) -> list[dict]:
        """列出最近的 Run（按创建时间倒序）。"""
        if not os.path.isdir(self.runs_dir):
            return []
        # 轻量路径：只解析 run.json，不构造 Run、不碰事件流。
        # 原实现对每个 run 调 get() 然后才截断到 limit，于是列表耗时随
        # 历史总量线性增长，而用户看到的永远只有前 50 条。
        # 必须遍历全部再精排，不能用 mtime 预筛。
        # 需要注意：mtime 与 created_ts 严重背离——某些 run 的 mtime 排名
        # 300+（最旧），created_ts 却在前 20。用 mtime 粗排取候选会**丢掉
        # 最新的任务**，而用户打开列表就是为了看最新的。正确性优先于速度。
        items = []
        for name in os.listdir(self.runs_dir):
            if not _is_hex_id(name):
                continue
            m = self._read_meta(name)
            if m is None:
                continue
            # seq 字段缺失说明是缓存引入之前写的 run：必须回退读事件流，
            # 否则列表里的 seq 恒为 0，前端会每次从头重拉整份日志。
            seq = m.get("seq")
            if seq is None:
                seq = _replay_seq(os.path.join(self.runs_dir, name,
                                               "events.jsonl"))
            status = m.get("status", "queued")
            phase = m.get("phase", "queued")
            # 与 get() 同一条归位规则：列表走轻量路径只读 run.json，
            # 不做归位的话列表页会一直显示"排队中"，而点进去详情却是
            # "已中断"——同一个任务两个页面给出不同结论。
            if status not in TERMINAL and not is_active(name):
                status = "interrupted"
            items.append({
                "id": name,
                "status": status,
                "phase": phase,
                "progress": 100 if status in TERMINAL else _progress_of(phase),
                "seq": int(seq or 0),
                "meta": {k: v for k, v in m.items()
                         if k not in ("id", "status", "phase", "progress",
                                      "seq", "updated_ts")},
                "created_ts": m.get("created_ts", 0),
            })
        items.sort(key=lambda x: x.get("created_ts") or 0, reverse=True)
        return items[:limit]
