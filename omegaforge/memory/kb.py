"""OmegaForge KnowledgeBase — 个人知识库 / 长期记忆（零依赖，纯 stdlib）。

设计：
* 文档制存储（id/type/title/text/tags/ts），type ∈ note|wiki|memory|doc
* 检索：CJK 二元组 + 英文词元的 TF 计分（无向量库依赖，离线可用；
  v0.4 路线图提供可选的向量召回适配器）
* 持久化：单文件 JSON（OMEGAFORGE_HOME/kb.json），每次写盘原子替换
* memory 是 KB 的一等公民：remember/recall 即 add(type=memory)/search
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from typing import Optional

from ..core.atomicio import atomic_write_json, file_lock
from ..core.errors import UserError
from ..core.paths import LazyHome

# 单条文档正文上限。没有上限的话，一条超长正文会原样落盘，此后每次保存
# 全量重写、每次加载全量进内存——一条数据就能把个人知识库变成内存与 IO
# 放大器。超限给明确提示（可自助拆分），而不是静默截断（截断等于悄悄丢
# 用户内容）。
MAX_DOC_CHARS = 200_000


def _tokens(text: str) -> list[str]:
    """CJK bigram + latin word tokenizer."""
    text = (text or "").lower()
    out: list[str] = []
    for w in re.findall(r"[a-z0-9_]{2,}", text):
        out.append(w)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    out += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return out


def _clean_tags(tags) -> list[str]:
    """把任意入参规整成 list[str]。

    为什么必须有这一层：
    之前 add() 原样存 tags。`POST /api/kb/add {"tags":123}` 会被 200 接受；
    之后 search() 里 `" ".join(d.get("tags", []))` 对 int 抛
    `TypeError: can only join an iterable`。该异常发生在 do_GET 内部且当时
    do_GET 无兜底，服务端线程直接崩、连接被掐断不返回任何响应 ——
    用户表现为"知识库一搜就转圈/网络错误"，而且**没有任何 API 能删掉这条
    脏数据**（没有 /api/kb/delete），只能手工改 kb.json 才能恢复。
    规整规则：None→[]；str→[str]；可迭代→逐项 str(); 其他→[]。
    """
    if tags is None:
        return []
    if isinstance(tags, str):
        return [tags]
    if isinstance(tags, (list, tuple, set)):
        return [str(t) for t in tags]
    return []


def _tag_text(tags) -> str:
    """search 侧的兜底：即便历史脏数据已落盘，也绝不让 join 抛异常。"""
    try:
        return " ".join(_clean_tags(tags))
    except Exception:                                # noqa: BLE001
        return ""


def _safe_str(raw, default: str = "") -> str:
    """title/text 收敛成字符串。

    当文档中 title 或 text 为空时，search() 里的
    `d.get("title","") + " \n " + d.get("text","")` 会把 None 拼进字符串，
    抛 TypeError，最终表现为服务端错误。None 只有在键缺失时才会被默认值
    兜住，键存在但值为 null 依然会触发。
    """
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, str):
        return raw
    try:
        return str(raw)
    except Exception:                                # noqa: BLE001
        return default


def _safe_ts(raw) -> float:
    """ts 收敛成 float：字符串/None/NaN 一律 0（排最后，不参与崩溃竞争）。

    时间戳若为字符串，`-d.get("ts", 0)` 对字符串取负会抛 TypeError。
    """
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


def _safe_limit(raw, default: int) -> int:
    """limit 收敛成正整数：'5' / None / [1]  previously 直击切片抛 TypeError。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


class KnowledgeBase(LazyHome):
    def __init__(self, home: Optional[str] = None):
        super().__init__(home)
        self._docs: dict = {}
        # 多线程互斥。服务端是 ThreadingHTTPServer，两个并发请求就可能同时
        # 改同一个字典；没有它，json.dump 迭代途中被改动会抛
        # "dictionary changed size during iteration"。
        self._lock = threading.RLock()
        self.ensure_loaded()

    @property
    def path(self) -> str:
        return os.path.join(self.home, "kb.json")

    # -- persistence ---------------------------------------------------
    def _reload(self) -> None:
        # 先无条件清空：home 切到新目录而该目录还没有 kb.json 时，若不清空，
        # 内存里旧 home 的条目会残留并被当成新 home 的数据——一旦 _save
        # 触发就整份写进新目录（例如：全新 tmp 目录里凭空出现 8 条旧条目）。
        #
        # Tasks._reload 有这句、KB 漏了：「同族实现只有一个成员漏」是本项目
        # 反复出现的失效形态（as_float/as_int、sed -i 都栽在同一处）。
        self._docs = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._docs = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._docs = {}
        # 历史文件可能被手改成 list / str；后续 .values() 会直接 AttributeError，
        # 且该异常发生在 do_GET 里会让整条连接断开。这里统一收口成 dict。
        if not isinstance(self._docs, dict):
            self._docs = {}
        # dict 整体合规不代表里面的值合规：例如 {"a": "我不是字典"} 会让
        # search()/all() 的 d.get(...) 抛 AttributeError -> 500。不可用的
        # 条目就地剔除，并顺手把可救的字段收敛好（ts/title/text/tags）。
        self._docs = {k: self._norm(v) for k, v in self._docs.items()
                      if isinstance(v, dict)}

    @staticmethod
    def _norm(d: dict) -> dict:
        d["id"] = _safe_str(d.get("id"), "")
        d["type"] = _safe_str(d.get("type"), "note")
        d["title"] = _safe_str(d.get("title"))
        d["text"] = _safe_str(d.get("text"))
        d["tags"] = _clean_tags(d.get("tags"))
        d["ts"] = _safe_ts(d.get("ts"))
        return d

    def _save(self) -> None:
        os.makedirs(self.home, exist_ok=True)
        # 快照而非 self._docs 本身：json.dump 会迭代字典，序列化途中被别的
        # 线程插入会抛 RuntimeError。调用方已持锁，快照是第二道保险。
        atomic_write_json(self.path, dict(self._docs))

    @contextlib.contextmanager
    def _mutate(self):
        """读-改-写临界区：线程锁 + 跨进程锁 + 写前重读。

        只把 _save 改成原子写**挡不住丢更新**：每个写入者内存里都是自己
        那份全量快照，最后写的会整体覆盖前面的，并发写入只有 1~2 条存活。所以必须在改动前**重新读盘**，把别的写入者刚落盘的
        内容并进来。
        """
        with self._lock:
            with file_lock(self.path):
                self._reload()
                self._loaded_home = self.home
                yield

    # -- CRUD ----------------------------------------------------------
    def add(self, title: str, text: str, type: str = "note",   # noqa: A002
            tags: Optional[list] = None, doc_id: Optional[str] = None) -> str:
        doc_id = doc_id or uuid.uuid4().hex[:10]
        title = _safe_str(title)
        text = _safe_str(text)
        if len(text) > MAX_DOC_CHARS:
            raise UserError(
                f"内容过长（{len(text)} 字，上限 {MAX_DOC_CHARS} 字），"
                "请拆分后再存入知识库")
        with self._mutate():
            self._docs[doc_id] = {
                "id": doc_id, "type": _safe_str(type, "note"), "title": title,
                "text": text, "tags": _clean_tags(tags), "ts": time.time()}
            self._save()
        return doc_id

    def get(self, doc_id: str) -> Optional[dict]:
        self.ensure_loaded()
        return self._docs.get(doc_id)

    def delete(self, doc_id: str) -> bool:
        with self._mutate():
            if doc_id in self._docs:
                del self._docs[doc_id]
                self._save()
                return True
        return False

    def all(self, type: Optional[str] = None) -> list:         # noqa: A002
        self.ensure_loaded()
        docs = [d for d in self._docs.values() if isinstance(d, dict)]
        if type:
            docs = [d for d in docs if d.get("type") == type]
        return sorted(docs, key=lambda d: -_safe_ts(d.get("ts")))

    def count(self) -> int:
        self.ensure_loaded()
        return len(self._docs)

    # -- search ----------------------------------------------------------
    def search(self, query: str, type: Optional[str] = None,    # noqa: A002
               limit: int = 5) -> list[dict]:
        q = _tokens(query)
        if not q:
            return []
        limit = _safe_limit(limit, 5)
        self.ensure_loaded()
        results = []
        for d in self._docs.values():
            if not isinstance(d, dict):
                continue
            if type and d.get("type") != type:
                continue
            body = _tokens(_safe_str(d.get("title")) + " \n "
                           + _safe_str(d.get("text")) + " \n "
                           + _tag_text(d.get("tags")))
            if not body:
                continue
            hits = sum(1 for t in q if t in body)
            if hits == 0:
                continue
            score = hits / len(q) * (1 + 0.1 * body.count(q[0]))
            # title 命中加权
            title_hits = sum(1 for t in q
                             if t in _tokens(_safe_str(d.get("title"))))
            score += 0.25 * title_hits / max(1, len(q))
            results.append({"id": d.get("id", ""), "type": d.get("type"),
                            "title": d.get("title"), "score": round(score, 3),
                            "excerpt": _safe_str(d.get("text"))[:160]})
        results.sort(key=lambda r: -r["score"])
        return results[:limit]

    # -- memory sugar ------------------------------------------------------
    def remember(self, fact: str, tags: Optional[list] = None) -> str:
        fact = _safe_str(fact)
        return self.add(title=fact[:60], text=fact, type="memory", tags=tags)

    def recall(self, query: str, limit: int = 3) -> list[dict]:
        return self.search(query, type="memory", limit=limit)
