#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接线守卫：前端调用的每个接口必须真被后端服务，且真被界面用到。

## 为什么需要这一条

"接口能通"和"用户点了有反应"是两件事，中间隔着两层接线：

  后端有这个路由  ←→  前端调了这个路径  ←→  某个页面真的调了这个函数

只验前一层时，下面两类失效全绿：

  1. 前端调了一个后端没有的路径 —— 点下去 404，界面上表现为"没反应"。
     而功能验收是照后端端点清单打的，根本不会碰到前端这条路径。
  2. api.ts 里写了函数、没有任何页面引用 —— 这个能力在界面上不存在。
     后端端点照常被验收通过，用户却找不到入口。

这两类都是典型的"假功能"：后端真的有、测试真的绿、用户真的用不了。

## 判定口径

  - 后端路由从 server.py 真实提取（`==` 精确与 `startswith` 前缀两类），
    不从文档或手写清单取：写死的清单会与实际路由漂移，而漂移后的症状
    与"路由真的没了"无法区分。
  - 前端路径从 api.ts 真实提取，模板串里的 `${...}` 归一为前缀。
  - 提取不到任何一侧时判失败，不判通过：空集合会让比对恒等成立。

退出码 0 = 接线完整；1 = 有断头。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "omegaforge", "server.py")
API_TS = os.path.join(ROOT, "frontend", "src", "lib", "api.ts")
PAGES = os.path.join(ROOT, "frontend", "src", "pages")
COMPONENTS = os.path.join(ROOT, "frontend", "src", "components")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass


def backend_routes() -> tuple[set[str], set[str]]:
    """返回 (精确路由, 前缀路由)。"""
    try:
        with open(SERVER, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        raise SystemExit(f"::error::读不到 server.py（{e}）——读不到不能判通过")

    exact = set(re.findall(r'path\s*==\s*"(/api/[^"]*)"', src))
    exact |= set(re.findall(r'"(/api/[^"]*)"\s*==\s*path', src))
    # startswith 既可能是单串，也可能是元组
    prefixes = set(re.findall(r'path\.startswith\(\s*"/?(api/[^"]*)"', src))
    for m in re.findall(r'path\.startswith\(\(\s*([^)]*?)\s*\)\)', src):
        prefixes |= set(re.findall(r'"(/api/[^"]*)"', m))
    # 元组里可能写成不带前导斜杠
    prefixes = {p if p.startswith("/") else "/" + p for p in prefixes}
    return exact, prefixes


def frontend_calls() -> dict[str, list[str]]:
    """返回 {导出符号: [路径...]}，路径已把模板插值归一为前缀。"""
    try:
        with open(API_TS, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        raise SystemExit(f"::error::读不到 api.ts（{e}）——读不到不能判通过")

    # 按导出符号切成块：从 "export ... 名字" 到下一个 export 之前
    marks = list(re.finditer(r'^export\s+(?:async\s+)?(?:function|const)\s+([A-Za-z0-9_]+)',
                             src, re.M))
    out: dict[str, list[str]] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(src)
        body = src[m.start():end]
        paths: list[str] = []
        for raw in re.findall(r'[`\'"]/(api/[^`\'"]*)[`\'"]', body):
            # 模板里的 ${...} 通常是 id：归一为前缀，后端侧按 startswith 匹配
            # 模板里的 ${...} 通常是 id：归一为前缀，后端侧按 startswith 匹配
            p = "/" + re.sub(r'\$\{[^}]*\}', '', raw)
            # 查询串不是路径的一部分：后端按 path 匹配，? 之后是 query。
            # 不剥掉会把 /api/wiki/page?slug= 判成后端没有的路径。
            p = p.split("?", 1)[0]
            paths.append(p)
        out[m.group(1)] = paths
    return out


def ui_symbols() -> set[str]:
    """pages/ 与 components/ 下出现过的标识符集合（含 import 与直接使用）。"""
    names: set[str] = set()
    for base in (PAGES, COMPONENTS):
        if not os.path.isdir(base):
            continue
        for dirpath, _d, files in os.walk(base):
            for fn in files:
                if not fn.endswith((".ts", ".tsx")):
                    continue
                with open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as f:
                    names |= set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', f.read()))
    return names


def served(path: str, exact: set[str], prefixes: set[str]) -> bool:
    if path in exact:
        return True
    return any(path.startswith(p) for p in prefixes)


def main() -> int:
    exact, prefixes = backend_routes()
    if not exact and not prefixes:
        print("::error::server.py 里没提取到任何路由 —— 提取逻辑失效，不能判通过")
        return 1
    calls = frontend_calls()
    if not calls:
        print("::error::api.ts 里没提取到任何导出 —— 提取逻辑失效，不能判通过")
        return 1

    bad: list[str] = []

    # 方向一：前端调的路径，后端必须真的有
    for sym, paths in sorted(calls.items()):
        for p in paths:
            if not served(p, exact, prefixes):
                bad.append(f"前端 {sym} 调用了后端没有的路径 {p} —— 点下去 404，界面上就是没反应")

    # 方向二：导出的函数必须有页面真的用，否则界面上没有这个入口
    used = ui_symbols()
    for sym in sorted(calls):
        if sym not in used:
            bad.append(f"api.ts 导出了 {sym} 但没有任何页面/组件引用 —— 该能力在界面上不存在")

    print(f"后端路由 精确 {len(exact)} / 前缀 {len(prefixes)}")
    print(f"前端导出 {len(calls)} 个符号")
    for b in bad:
        print(f"::error::{b}")
    if bad:
        print(f"\n接线断头 {len(bad)} 处")
        return 1
    print("OK 接线完整：前端每条路径后端都有，且每个导出都有界面引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
