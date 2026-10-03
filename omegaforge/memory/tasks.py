"""OmegaForge Tasks — 个人待办清单（tick），支持优先级与完成流。

脏数据约定
----------
tasks.json 是磁盘上的普通文件：较早版本本写的、手工改过的、别的进程截断的
都可能存在。而待办列表接口没有任何清理入口——一条脏记录能让任务页永久
打不开，用户只能手工改文件才能恢复。

脏数据会以多种形式出现（优先级越界、缺少完成标记、时间字段不是数值），
硬取值会直接抛异常，并被统一映射成内部错误——用户看到"服务器故障"，
实际只是某条记录格式不对。

所以这里一律走收敛而非硬取：脏值回落默认值并排到最后，脏记录不拖垮整页。
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from typing import Optional

from ..core.atomicio import atomic_write_json, file_lock
from ..core.errors import UserError
from ..core.paths import LazyHome


def _pri(raw) -> int:
    """优先级收敛到 1/2/3；越界或脏值一律按最低（3）处理，绝不 KeyError。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 3
    if v not in (1, 2, 3):
        return 3
    return v


def _done(raw) -> bool:
    """done 字段可能缺失或不是布尔（历史文件/手工改过），缺失按未完成。"""
    return bool(raw)


def _ts(raw) -> float:
    """时间戳收敛成 float；字符串/None/NaN 一律 0（排到最后而不是崩掉）。"""
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v == v and abs(v) != float("inf") else 0.0
    if isinstance(raw, str):
        try:
            v = float(raw.strip())
            return v if v == v else 0.0
        except ValueError:
            return 0.0
    return 0.0


def _text(raw) -> str:
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, str):
        return raw.strip()
    try:
        return str(raw).strip()
    except Exception:                                   # noqa: BLE001
        return ""


class Tasks(LazyHome):
    def __init__(self, home: Optional[str] = None):
        super().__init__(home)
        self._items: dict = {}
        self._lock = threading.RLock()
        self.ensure_loaded()

    @property
    def path(self) -> str:
        return os.path.join(self.home, "tasks.json")

    def _reload(self) -> None:
        self._items = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._items = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._items = {}
        # 历史文件可能被手改成 list / str；后续 .values() 会直接 AttributeError。
        if not isinstance(self._items, dict):
            self._items = {}
        # 载入即归一化：脏字段就地收敛（priority/done/created/text），
        # 这样 list() 返回给界面的也是合规值——否则界面拿到 priority="2"、
        # created="昨天" 这类原始脏值，前端再按数字排序/渲染同样会出问题。
        self._items = {k: self._norm(v) for k, v in self._items.items()
                       if isinstance(v, dict)}

    @staticmethod
    def _norm(it: dict) -> dict:
        it["id"] = _text(it.get("id"))
        it["text"] = _text(it.get("text"))
        it["done"] = _done(it.get("done"))
        it["priority"] = _pri(it.get("priority"))
        it["created"] = _ts(it.get("created"))
        return it

    def _save(self) -> None:
        os.makedirs(self.home, exist_ok=True)
        atomic_write_json(self.path, dict(self._items))

    @contextlib.contextmanager
    def _mutate(self):
        """读-改-写临界区：线程锁 + 跨进程锁 + 写前重读。

        理由同 KnowledgeBase._mutate。例如：本文件最严重：12 个并发 add
        只有 1~2 条存活，其余被静默丢弃——用户点完以为存上了，刷新就没了。
        """
        with self._lock:
            with file_lock(self.path):
                self._reload()
                self._loaded_home = self.home
                yield

    def add(self, text: str, priority: int = 2) -> dict:
        with self._mutate():
            return self._add_unlocked(text, priority)

    def _add_unlocked(self, text: str, priority: int) -> dict:
        if not text or not str(text).strip():
            # 抛 ValueError("task text required") —— 英文原文，
            # 且 ValueError 会被 _classify 压成泛化的「请求内容有误，请检查后重试」。
            # 用户填了纯空格，看到的却是和系统故障同级的模糊提示。
            # 改 UserError 后 CLI / 服务端 / 智能体工具三处同时拿到这句中文。
            raise UserError("请填写任务内容")
        # 入参同样收敛：非法取值直接转换会抛异常并冒泡成内部错误。服务端已
        # 挡一层，这里再挡一层是为了命令行与直接调用同样不失败。
        priority = _pri(priority)
        tid = uuid.uuid4().hex[:8]
        item = {"id": tid, "text": str(text).strip(), "done": False,
                "priority": priority, "created": time.time(),
                "done_ts": None}
        self._items[tid] = item
        self._save()
        return item

    def complete(self, task_id: str) -> bool:
        with self._mutate():
            it = self._items.get(task_id)
            if not isinstance(it, dict) or _done(it.get("done")):
                return False
            it["done"] = True
            it["done_ts"] = time.time()
            self._save()
            return True

    def reopen(self, task_id: str) -> bool:
        with self._mutate():
            it = self._items.get(task_id)
            if not isinstance(it, dict) or not _done(it.get("done")):
                return False
            it["done"] = False
            it["done_ts"] = None
            self._save()
            return True

    def delete(self, task_id: str) -> bool:
        with self._mutate():
            if task_id in self._items:
                del self._items[task_id]
                self._save()
                return True
        return False

    def list(self, scope: str = "pending") -> list[dict]:
        self.ensure_loaded()
        items = [i for i in self._items.values() if isinstance(i, dict)]
        if scope == "pending":
            items = [i for i in items if not _done(i.get("done"))]
        elif scope == "done":
            items = [i for i in items if _done(i.get("done"))]
        # 排序键全部走收敛函数：脏 priority/created 排到最后，不参与崩溃竞争
        return sorted(items, key=lambda i: (_pri(i.get("priority")),
                                            -_ts(i.get("created"))))

    def stats(self) -> dict:
        self.ensure_loaded()
        all_ = [i for i in self._items.values() if isinstance(i, dict)]
        return {"total": len(all_),
                "pending": sum(1 for i in all_ if not _done(i.get("done"))),
                "done": sum(1 for i in all_ if _done(i.get("done")))}
