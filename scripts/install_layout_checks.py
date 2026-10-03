# -*- coding: utf-8 -*-
"""安装产物的布局与内容校验（纯函数，供 CI 与沙盒两侧共用）。

## 为什么单独成模块

同一套判定要在两处执行：
  - CI 的 Windows runner：安装后的目录里跑（唯一能真启动 exe 的地方）
  - 沙盒：把安装包解包出来跑（沙盒装不了 exe，但能独立复查产物内容）

判定写在两处就会各自漂移，故收敛到这里，两边只负责取到目录。

## 每条断言都对应一类"装完才发现"的失效

这些失效的共同点是：源码目录里全绿，甚至打包也成功，只有装上才现形。
"""
from __future__ import annotations

import json
import os
import re
import socket

SIDECAR_PREFIX = "omegaforge-backend"

# 主程序名取自 tauri.conf.json 的 productName，不写死：改名后写死的常量
# 会一直"找不到主程序"，而该症状与"产物真的缺主程序"无法区分。
CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src-tauri", "tauri.conf.json")


def product_name() -> str:
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f).get("productName") or ""
    except (OSError, json.JSONDecodeError):
        return ""


def _norm(s: str) -> str:
    """归一化为只留字母数字：安装器会把 productName 里的空格写成 - 或 _，
    直接按 productName 全文比对会找不到主程序，而该症状与"产物真的缺主程序"
    无法区分。"""
    return re.sub(r"[^0-9a-z]", "", s.lower())


def find_file(root: str, prefix: str, suffix: str) -> str:
    """递归定位名字归一化后以 prefix 归一化形式开头、以 suffix 结尾的文件。"""
    np = _norm(prefix)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(suffix) and _norm(name).startswith(np):
                return os.path.join(dirpath, name)
    return ""


def check_layout(root: str, exe_suffix: str = ".exe") -> list[str]:
    """返回问题列表；空列表代表布局合格。"""
    bad: list[str] = []

    # 1. 主程序本体。缺了它，安装目录里只剩后端，用户双击没东西可开——
    #    而只验 sidecar 能启动的话这一条恒真。
    app = product_name()
    if not app:
        bad.append(f"读不到 productName（{CONF}）——无法判定主程序是否存在，"
                   f"读不到不能当作通过")
    else:
        found = find_file(root, app, exe_suffix)
        if not found:
            bad.append(f"安装目录未找到主程序 {app}*{exe_suffix}")
        elif os.path.getsize(found) == 0:
            bad.append(f"主程序为空文件: {found}")

    # 2. sidecar 与其同级 _internal
    exe = find_file(root, SIDECAR_PREFIX, exe_suffix)
    if not exe:
        bad.append(f"安装目录未找到 {SIDECAR_PREFIX}*{exe_suffix}")
        return bad
    if os.path.getsize(exe) == 0:
        bad.append(f"sidecar 为空文件: {exe}")

    internal = os.path.join(os.path.dirname(exe), "_internal")
    if not os.path.isdir(internal):
        bad.append("sidecar 与 _internal 不同级 —— 启动必报 "
                   "Failed to load Python shared library")
        return bad

    # 3. _internal 必须非空且含 Python 运行时。
    #    空目录同样会让"同级"判定成立，而启动时缺的是运行库。
    entries = os.listdir(internal)
    if not entries:
        bad.append("_internal 为空目录：同级判定成立但无运行时依赖")
        return bad
    if not any(re.match(r"python\d+\.dll$", n) or n.startswith("libpython")
               for n in entries):
        bad.append("_internal 缺少 Python 运行时（python*.dll）")

    # 4. 主程序必须带着前端产物。
    #    前端没打进去时，应用能启动、接口也通，但界面是全白——
    #    "能启动"与"功能验收"两条都验不到它。
    if app:
        app_path = find_file(root, app, exe_suffix)
        if app_path:
            fe = frontend_assets_embedded(app_path)
            if not fe:
                bad.append("主程序内未找到前端产物（index.html / assets/*.js）——"
                           "装上打开会是白屏，而启动与接口验收都发现不了")

    return bad


def frontend_assets_embedded(app_path: str) -> bool:
    """在主程序二进制里找前端产物痕迹（只做存在性判断，不解压）。"""
    try:
        with open(app_path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    has_html = b"index.html" in data or b"<!doctype html" in data.lower()
    # tauri 把 dist 打进二进制并压缩，路径串可能被压掉，故按扩展名兜底
    has_js = data.count(b".js") > 0
    return has_html and has_js


def free_port(start: int = 8792, tries: int = 40, host: str = "127.0.0.1") -> int:
    """从 start 起找一个当前未被监听的端口。

    启动前若沿用被占用的端口，就绪探测会连到别的进程，判定恒真；
    直接判失败又会让环境差异（runner 上恰好有服务占着）记成产品问题。
    故这里换一个空闲端口继续，而不是二选一。
    """
    for i in range(tries):
        p = start + i
        if not port_in_use(p, host):
            return p
    return 0


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()
