# -*- coding: utf-8 -*-
"""用户可见指引的目标可达性判定。

## 守的是什么

面向用户的报错常常以「请在 X 里做 Y」收尾。这类指引成立的前提是 X 真实
存在且那里真的能做 Y。X 若是不存在的页面、或页面里根本没有 Y 这个动作，
用户会被引到一个找不到的地方，反复重试也不会成功——而接口层面一切正常，
功能验收只看接口通不通，看不出指引是空的。

## 判定方式

只认中文文案里的「在 <位置> 的 <名称> 面板」这一形态，把位置与名称取出来，
再回到前端源码里查：该位置对应的页面是否引用了名称对应的面板组件。
查不到即判为不可达。
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 「在设置页的语音面板」「在设置里」——取位置与面板名
TARGET_RE = re.compile(r"在(设置页|设置)(?:的|里)?([\u4e00-\u9fff]{1,8}?)面板")

# 位置 → 前端页面文件
PAGE_OF = {
    "设置页": "frontend/src/pages/SettingsPage.tsx",
    "设置": "frontend/src/pages/SettingsPage.tsx",
}

# 中文面板名 → 组件名的英文关键词。
# 文案说中文、组件名是英文，没有这张表就永远匹配不上；而匹配不上若判成
# "可达"，整条守卫就恒真。故表里没有的面板名一律判"无法判定"，并明确报出。
PANEL_ALIASES = {
    "语音": ["voice"],
    "权限": ["permission"],
    "供应商": ["provider"],
    "模型": ["model"],
}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def extract_targets(text: str) -> list[tuple[str, str]]:
    """从一条用户可见文案里取出指引目标：[(位置, 面板名), ...]。"""
    return TARGET_RE.findall(text)


def panel_components(page_src: str) -> list[str]:
    """页面源码里 import 的面板组件名。

    只认从组件目录导入、且首字母大写的名字：接口函数名（fetchProviders
    之类）同样含关键词，把它们算作"面板存在"会让不可达的指引判成可达。
    """
    out: list[str] = []
    for m in re.finditer(
            r'import\s*\{([^}]+)\}\s*from\s*["\']@/components/[^"\']+["\']',
            page_src):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name and name[0].isupper():
                out.append(name)
    return out


def unreachable(text: str) -> list[str]:
    """返回不可达的指引目标说明；空列表代表指引都指向真实存在的入口。"""
    bad: list[str] = []
    for place, panel in extract_targets(text):
        page_rel = PAGE_OF.get(place)
        if not page_rel:
            continue
        page = os.path.join(ROOT, page_rel)
        src = _read(page)
        if not src:
            bad.append(f"指引指向「{place}」但读不到 {page_rel} —— 读不到不能当作可达")
            continue
        comps = panel_components(src)
        keys = PANEL_ALIASES.get(panel)
        if not keys:
            bad.append(
                f"指引指向「{place}的{panel}面板」，但中文面板名「{panel}」未登记英文别名"
                f"——无法判定可达性，未登记不能当作可达")
            continue
        hit = [c for c in comps
               if any(k in c.lower() for k in keys)]
        # 页面里没有名字含该关键词的面板组件：指引指向的入口不存在
        if not hit:
            bad.append(
                f"指引指向「{place}的{panel}面板」，但 {page_rel} 未引用匹配 {keys} 的组件"
                f"（现有：{comps or '无'}）")
    return bad


def panel_provides_action(panel_rel: str, kind: str) -> bool:
    """面板是否真的提供了某类可行动作。

    kind:
      install —— 给出可执行的安装命令
      source  —— 给出模型/资源的获取来源链接
    """
    src = _read(os.path.join(ROOT, panel_rel))
    if not src:
        return False
    if kind == "install":
        return bool(re.search(r"pip\s+install\s+[A-Za-z0-9_.\-]+", src))
    if kind == "source":
        return bool(re.search(r"https?://[^\s\"']+", src))
    return False
