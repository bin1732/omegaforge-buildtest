#!/usr/bin/env python3
"""逐页渲染判定：真实浏览器点击后的判定规则。

判定单独成模块，是为了让"脚本能跑"与"判定会抓"分开验证：脚本在沙盒里
对真实产物跑，判定在用例里对合成事件跑。两者共用同一份规则，不会出现
"线上判通过、本地判失败"这类漂移。

界面没有 URL 路由，页面只能靠点击切换。只读首屏的核查覆盖不到其余
九页——页面渲染时抛错会表现为空内容，而首屏始终正常，于是"首屏能画
出来"会被误读成"十个页面都好"。
"""
from __future__ import annotations

from typing import Any, Dict, List

EXPECTED_PAGES: List[str] = [
    "蒸馏工坊", "对话", "知识库", "待办", "技能与人设",
    "竞技场", "基因组", "运行记录", "用量", "设置",
]

MIN_NODES = 5
MIN_TEXT = 10


def evaluate(events: List[Dict[str, Any]]) -> List[str]:
    """把浏览器驱动产出的一串事件折算成失败清单，空列表表示通过。"""
    fails: List[str] = []
    pages = [e for e in events if e.get("event") == "page"]
    nav = next((e for e in events if e.get("event") == "nav"), None)

    if nav is None:
        fails.append("未取到侧边导航，逐页核查无从判定")
        return fails
    labels = list(nav.get("labels") or [])
    missing = [p for p in EXPECTED_PAGES if p not in labels]
    if missing:
        fails.append("导航缺少页面: " + "、".join(missing))
    if len(pages) < len(EXPECTED_PAGES):
        fails.append("点击后只得到 %d 页，少于 %d" % (len(pages), len(EXPECTED_PAGES)))

    for p in pages:
        label = p.get("label")
        if p.get("error"):
            fails.append("[%s] %s" % (label, p["error"]))
            continue
        if p.get("current") != label:
            fails.append("[%s] 点击后当前页标记为 %r" % (label, p.get("current")))
        nodes = int(p.get("nodes") or 0)
        if nodes < MIN_NODES:
            fails.append("[%s] 主区仅 %d 个节点，疑似空白" % (label, nodes))
        text_len = int(p.get("textLen") or 0)
        if text_len < MIN_TEXT:
            fails.append("[%s] 文案仅 %d 字，疑似未渲染" % (label, text_len))
        for err in p.get("errors") or []:
            # console: 与 pageerror: 都要算。只认后者的话，错误边界接住异常
            # 并渲染出一块内容够多的兜底界面时，节点与文案两项都达标，
            # 页面已坏却判通过。
            fails.append("[%s] 控制台报错: %s" % (label, str(err)[:200]))

    # 各页必须互不相同：点击没生效时十页会给出同一份内容
    seen: Dict[str, List[str]] = {}
    for p in pages:
        s = str(p.get("sample") or "").strip()
        if not s:
            continue
        seen.setdefault(s, []).append(str(p.get("label")))
    for s, group in seen.items():
        if len(group) > 1:
            fails.append("多页内容完全相同（点击疑似未生效）: " + "、".join(group))
    return fails


def healthy_event(label: str) -> Dict[str, Any]:
    """构造一条"该页正常"的事件，供用例做反向样本。"""
    return {
        "event": "page", "label": label, "current": label,
        "nodes": 40, "textLen": 120, "bodyNodes": 200,
        "sample": label + " 页面内容", "errors": [],
    }
