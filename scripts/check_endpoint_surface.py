# -*- coding: utf-8 -*-
"""后端能力的出口守卫：每个对外能力都必须有用户可达的出口。

## 守的是什么

后端实现了一个能力并挂了路由，若前端没有任何入口，那么：

    接口验收全绿（路由在、返回 JSON、不为空）
    命令行可用
    用户在界面上找不到它

这就是"假功能"——能力真实存在，只是没有出口。接口层与命令行层都验不到
这一点，因为它们测的是"能不能调"，不是"用户够不够得着"。

## 判定

1. 从后端取全部 `/api/...` 路由
2. 到前端源码里查有没有出现（含模板串拼接出的路径）
3. 查不到时，要求该端点已登记为「不经界面」，并写明理由
4. 已登记却又在前端出现的，视为陈旧登记，同样报出

第 4 条为什么单独守：只查"缺入口"的话，登记会变成逃避口——
随便登记一下就永远通过。反过来，登记了却又加了入口，
说明登记已过期，而过期登记会让"其实需要入口"的端点继续被当成不需要。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "omegaforge", "server.py")
FRONTEND_SRC = os.path.join(ROOT, "frontend", "src")

# 登记为「不经界面」的端点：必须写明理由。
# 没有理由的登记等于逃避——故理由为空同样判失败。
CLI_ONLY: dict[str, str] = {
    "/api/tools/exec":
        "命令执行类端点：经工具权限与危险命令多重判定后方可调用，"
        "界面不提供直接触发入口（权限面板只做授权，不做执行）",
    "/api/tools/policy":
        "策略读取：与 /api/tools/permissions 同源，"
        "界面统一走 permissions 面板，避免两处入口显示不一致",
}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def backend_routes() -> list[str]:
    """后端源码里出现的 `/api/...` 路由字面量，去重保序。"""
    src = _read(SERVER)
    out: list[str] = []
    for m in re.finditer(r'"(/api/[A-Za-z0-9_/\-]+)"', src):
        p = m.group(1)
        if p not in out:
            out.append(p)
    return out


def frontend_source() -> str:
    parts: list[str] = []
    for r, _d, fs in os.walk(FRONTEND_SRC):
        for f in fs:
            if f.endswith((".ts", ".tsx")):
                parts.append(_read(os.path.join(r, f)))
    return "\n".join(parts)


def has_frontend_entry(route: str, src: str) -> bool:
    """前端源码是否提到了该路由。

    既认整条字面量，也认模板串拼接：`/api/jobs/${id}` 这类写法里，
    路由前缀是被拼出来的，只比整条字面量会把它们全判成"没有入口"。
    """
    if route in src:
        return True
    tail = route[5:] if route.startswith("/api/") else route
    # 整条出现（后面接引号或问号）
    if re.search(re.escape(tail) + r'["\'`?]', src):
        return True
    # 末段是动态拼接：`/api/report/${id}` 对应路由 /api/report/<job>。
    # 只比整条字面量会把这类写法全判成"没有入口"。
    if "/" in tail:
        head = tail.rsplit("/", 1)[0]
        if re.search(re.escape(head) + r'/\$\{[^}]*\}', src):
            return True
        if re.search(re.escape(head) + r'/["\'`]', src):
            return True
    # 目录式路由 /api/tools/ 与其下任意子路径
    if route.endswith("/"):
        return re.search(re.escape(tail) + r'[A-Za-z0-9_/\-]*["\'`?]', src) is not None
    return False


def problems() -> list[str]:
    """返回全部问题；空列表代表每个对外能力都有可达出口。"""
    src = frontend_source()
    if not src:
        return ["读不到前端源码 —— 读不到不能当作无缺口"]
    routes = backend_routes()
    if not routes:
        return ["读不到后端路由 —— 读不到不能当作无缺口"]

    bad: list[str] = []
    for r in routes:
        used = has_frontend_entry(r, src)
        reg = CLI_ONLY.get(r)
        if used and reg is not None:
            bad.append(f"{r} 已登记为不经界面，但前端已有入口 —— 登记已陈旧")
            continue
        if used:
            continue
        if reg is None:
            bad.append(f"{r} 前端无入口且未登记 —— 能力存在但用户够不着")
        elif not reg.strip():
            bad.append(f"{r} 登记了不经界面但理由为空 —— 无理由的登记等于逃避")
    return bad


def main() -> int:
    bad = problems()
    if bad:
        print("后端能力缺少可达出口：")
        for b in bad:
            print("  -", b)
        return 1
    print(f"后端 {len(backend_routes())} 个路由，均有可达出口或已登记理由")
    return 0


if __name__ == "__main__":
    sys.exit(main())
