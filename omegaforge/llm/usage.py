"""OmegaForge UsageStore — 全局 token 用量账本（可持久化、可聚合）。

账本是追加式文件，历史上写进去的东西不可信：可能是较早版本本写的、可能是
上游把 token 返回成字符串（例如 "12" + "7" → "127"，账目虚高 10 倍）、
可能是负数、也可能是一行手工改坏的 JSON。

聚合视图是**整个用量页唯一的读路径**，它一崩，用户看到的就是
「操作失败，请稍后重试」，且没有任何清理入口——等于用量页永久打不开。
所以读取侧的原则是：单条脏记录跳过，绝不连累全局。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from typing import Any, Optional

from ..core.paths import LazyHome


def _nonneg_int(raw: Any, default: int = 0) -> int:
    """把任意来源的值收敛成非负整数——读取侧的兜底，不抛异常。

    与 validate.as_int 的分工：as_int 用于**入口**，类型非法要变成 400
    让用户改；这里用于**读盘**，脏数据只能跳过，不能让整个页面崩掉。
    bool 按 0 处理（它是 int 子类，但语义上不是用量）。
    """
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, int):
        return raw if raw > 0 else 0
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):   # NaN / inf
            return default
        return int(raw) if raw > 0 else 0
    if isinstance(raw, str):
        s = raw.strip()
        try:
            v = int(s)
        except ValueError:
            try:
                v = int(float(s))
            except ValueError:
                return default
        return v if v > 0 else 0
    return default


def _ts_float(raw: Any) -> Optional[float]:
    """时间戳收敛成 float；不可识别返回 None（该条不进时间线，但仍计入总量）。"""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v == v and abs(v) != float("inf") else None
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


class UsageStore(LazyHome):
    """追加式 JSONL 账本 + 聚合视图（阶段/模型/时间线）。"""

    def __init__(self, home: Optional[str] = None):
        # 无内存缓存（_entries 每次读盘），只需路径惰性，不必 ensure_loaded
        super().__init__(home)
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return os.path.join(self.home, "usage.jsonl")

    def record(self, phase: str, model: str, tokens: int,
               prompt: int = 0, completion: int = 0) -> None:
        # 写入侧同样夹紧：上游把 token 返回成负数时，账本会被"倒扣"，
        # 花得越多记得越少。这里统一归正，避免脏数据从源头进账。
        entry = {"ts": time.time(), "phase": str(phase or "?"),
                 "model": str(model or "?"),
                 "tokens": _nonneg_int(tokens),
                 "prompt": _nonneg_int(prompt),
                 "completion": _nonneg_int(completion)}
        try:
            os.makedirs(self.home, exist_ok=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _entries(self) -> list[dict]:
        """读取侧：**单条脏记录跳过，绝不连累全局**。

        为什么按字节读再逐行解码（见下）：
        直接按文本模式逐行迭代不行，因为 `UnicodeDecodeError` 是
        **ValueError 的子类，不是 OSError**，`except OSError` 接不住它。
        后果：

            账本里出现一个非 UTF-8 字节（磁盘损坏 / 跨版本写坏 / 手工改坏）
            GET /api/usage  → 400「操作失败，请稍后重试」
            GET /api/kb/list → 200 正常

        也就是说：**用量页永久打不开，而用户没有任何清理入口**——用量页
        本身就是那个入口，它挂了就无处可清。这正是文件头 docstring 自己
        警告的场景，但检查只写了一半。

        改为按字节读 + 单行 decode(errors="replace")：坏字节变成 U+FFFD，
        该行随后被 json.loads 判为坏行跳过，**其余行全部保留**。
        注意不能用 `except ValueError` 整体兜住——那会在坏行处中断迭代，
        坏行之后的所有记录一起丢失，与"绝不连累全局"相反。
        """
        if not os.path.exists(self.path):
            return []
        out = []
        try:
            with open(self.path, "rb") as f:
                for raw in f:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # 整行不是对象（例如较早版本本写的数组/裸数字）直接跳过
                    if isinstance(obj, dict):
                        out.append(obj)
        except OSError:
            pass
        return out

    def summary(self, buckets: int = 24, bucket_seconds: int = 300) -> dict:
        entries = self._entries()
        now = time.time()
        by_phase: dict = defaultdict(int)
        by_model: dict = defaultdict(int)
        timeline: list = [0] * buckets
        phase_time: dict = defaultdict(lambda: [0] * buckets)
        total = 0
        for e in entries:
            tk = _nonneg_int(e.get("tokens", 0))
            ph = str(e.get("phase") or "?")
            mo = str(e.get("model") or "?")
            total += tk
            by_phase[ph] += tk
            by_model[mo] += tk
            ts = _ts_float(e.get("ts"))
            if ts is None:
                continue                    # 时间戳不可识别：计入总量，不进时间线
            age = now - ts
            idx = buckets - 1 - int(age // bucket_seconds)
            if 0 <= idx < buckets:
                timeline[idx] += tk
                phase_time[ph][idx] += tk
        top_phases = dict(sorted(by_phase.items(), key=lambda kv: -kv[1])[:8])
        hottest = max(by_phase.items(), key=lambda kv: kv[1])[0] if by_phase else "-"
        return {"total": total, "entries": len(entries),
                "by_phase": top_phases, "by_model": dict(by_model),
                "timeline": timeline,
                "bucket_seconds": bucket_seconds,
                "heatmap": {p: v for p, v in phase_time.items()
                            if p in top_phases},
                "hottest_phase": hottest}
