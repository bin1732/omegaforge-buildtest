"""OmegaForge Wiki — markdown 页面 + 双链 + 反链 + 检索。

* slug 即地址（kebab-case），body 为 markdown
* [[slug]] 或 [[slug|别名]] 建立双链
* backlinks(slug) 反向索引：哪些页面链接到它
* 存储：OMEGAFORGE_HOME/wiki/*.md（frontmatter 两行 title/updated）
"""
from __future__ import annotations

import contextlib
import os
import re
import time
from typing import Optional

from .kb import _tokens
from ..core.atomicio import file_lock
from ..core.errors import UserError
from ..core.paths import LazyHome

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,60}$")
_LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9\-_]{0,60})(?:\|[^\]]+)?\]\]")


def _safe_str(raw, default: str = "") -> str:
    """把任意入参收敛成字符串：None 不再被 f-string 写成字面量 "None"。"""
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, str):
        return raw
    try:
        return str(raw)
    except Exception:                                   # noqa: BLE001
        return default


class Wiki(LazyHome):
    def __init__(self, home: Optional[str] = None, kb: Optional["object"] = None):
        super().__init__(home)
        self.kb = kb

    @property
    def dir(self) -> str:
        return os.path.join(self.home, "wiki")

    def _path(self, slug: str) -> Optional[str]:
        """slug → 文件路径；非法 slug 一律返回 None。

        为什么 _path 这一层就要卡住（确认的越界读）：
        标识校验必须下沉到路径解析这一层：若只在保存时校验，读取与删除
        直接拼接用户传入的标识，就能越过词条目录去读写别处的文件。
        校验下沉后，读写删三条路径一起收口，不可能再漏。
        """
        slug = (slug or "").strip().lower()
        if not _SLUG_RE.match(slug):
            return None
        return os.path.join(self.dir, slug + ".md")

    @contextlib.contextmanager
    def _mutate(self, slug: str):
        """同一词条的读-改-写临界区。

        例如：wiki 是唯一没有任何锁的持久化组件。save 是裸
        `open(..., "w")`——不是 atomic_write，写一半崩溃就留下半截文件；
        delete 与 save 并发时，删除后 save 又把文件写回来（与对话"复活"
        同一形态）。
        关键：**单侧加锁等于没加**。save 加锁而 delete 不加（或反过来）
        两把锁守的不是同一个临界区，竞争照旧。所以两者必须共用这里。
        """
        p = self._path(slug)
        if not p:                       # 非法 slug：交由 save/delete 自行报错
            yield
            return
        os.makedirs(self.dir, exist_ok=True)
        with file_lock(p):
            yield

    def _load_page(self, slug: str) -> Optional[dict]:
        p = self._path(slug)
        if not p or not os.path.exists(p):
            return None
        # errors="replace"：wiki 目录里只要有一个非 UTF-8 的 .md（旧编码文件、
        # 被误放进去的二进制），严格解码会抛 UnicodeDecodeError，而 pages()
        # 是列表页——整页 500，用户连删除入口都没有。
        with open(p, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        lines = raw.split("\n")
        title, body = slug, raw
        if lines and lines[0].startswith("title: "):
            title = lines[0][7:]
            body = "\n".join(lines[2:])
        return {"slug": slug, "title": title, "body": body,
                "links": sorted({m.group(1) for m in _LINK_RE.finditer(body)})}

    # ------------------------------------------------------------------
    def save(self, slug: str, title: str, body: str) -> str:
        slug = (slug or "").strip().lower()
        if not _SLUG_RE.match(slug):
            # 文案面向用户：slug 是用户在界面上填的地址，报错要说人话。
            # 抛 ValueError，_classify 把 ValueError 一律压成
            # 「请求内容有误，请检查后重试」——这句中文随即被吞掉，用户只知道
            # "有问题"，却不知道是地址格式不对（可当场改），还以为是系统故障。
            # 改 UserError 后文案原样透出，规则说明才真正到得了用户眼前。
            raise UserError(
                "词条地址只能包含小写字母、数字、连字符和下划线")
        # title 必须压成单行：frontmatter 是"按行"解析的（lines[0]），
        # 标题里带换行就能伪造出第二行、把内容塞进正文区——
        # title="标题\nupdated: 0\n\n伪造正文" 会让 get() 读出的 body
        # 以 "\n伪造正文" 开头，页面正文被整段顶替。
        # 空标题要回落到标识，否则会被格式化成字面量空值。
        title = " ".join(_safe_str(title).split()) or slug
        body = _safe_str(body)
        os.makedirs(self.dir, exist_ok=True)
        with self._mutate(slug):
            with open(self._path(slug), "w", encoding="utf-8") as f:
                f.write(f"title: {title}\nupdated: {time.time():.0f}\n\n{body}\n")
        if self.kb is not None:   # 同步入知识库索引，便于全局检索
            self.kb.add(title=f"wiki:{title}", text=body[:4000], type="wiki",
                        doc_id=f"wiki-{slug}")
        return slug

    def get(self, slug: str) -> Optional[dict]:
        page = self._load_page(slug)
        if page:
            page["backlinks"] = self.backlinks(page["slug"])
        return page

    def delete(self, slug: str) -> bool:
        # 与 save 共用 _mutate：并发 delete 时后到者会撞 FileNotFoundError，
        # 删除与写入竞争时文件会被写回来（"复活"）。
        with self._mutate(slug):
            p = self._path(slug)
            if p and os.path.exists(p):
                os.remove(p)
                return True
            return False

    def pages(self) -> list[dict]:
        out = []
        if not os.path.isdir(self.dir):
            return out
        for fn in os.listdir(self.dir):
            if fn.endswith(".md"):
                pg = self._load_page(fn[:-3])
                if pg:
                    out.append({"slug": pg["slug"], "title": pg["title"],
                                "links": pg["links"]})
        return sorted(out, key=lambda p: p["slug"])

    def backlinks(self, slug: str) -> list[str]:
        return [p["slug"] for p in self.pages()
                if slug in p["links"] and p["slug"] != slug]

    def search(self, query: str, limit: int = 5) -> list[dict]:
        q = _tokens(query)
        scored = []
        for p in self.pages():
            full = self._load_page(p["slug"])
            body = _tokens(p["title"] + "\n" + (full["body"] if full else ""))
            hits = sum(1 for t in q if t in body)
            if hits:
                scored.append({"slug": p["slug"], "title": p["title"],
                               "score": round(hits / len(q), 3)})
        scored.sort(key=lambda r: -r["score"])
        return scored[:limit]
