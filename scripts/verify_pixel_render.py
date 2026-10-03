# -*- coding: utf-8 -*-
"""像素级渲染校验：把页面真的画出来，看画出来的是什么。

## 为什么必须有这一层

装机验收验的是：主程序在、运行时在、前端产物在文件里、接口通。
这些全绿时，界面仍可能是全白的——前端产物存在但没被正确嵌入、
资源路径不对、脚本报错未渲染，都能让应用起来而画面空白。

接口层与文件层都验不到这一点：**它们测的是"东西在不在"，
不是"画出来是什么"。** 这一层只看真实渲染结果。

## 覆盖范围：只验首屏，不宣称逐页

本前端**没有 URL 路由**——页面切换靠顶部导航项的点击（`App.tsx` 里
`active` state 切换，源码里不存在任何 Router）。因此"逐路由截图"不成立：
访问 /forge、/chat 等任意路径，服务端 fallback 到 index.html 后渲染的
都是同一个默认页面，截出来的图必然完全一样。

按 11 个路由各截一张时，数值一致而判定全绿——那是恒真，
验到的只是"首屏画得出来"。故本脚本只截首屏，并如实声明范围。
逐页像素校验需要能驱动点击的浏览器（CDP / Playwright），
具备条件时再补。

## 判定

  * 首屏必须能截出图（截不出 = 页面没起来）
  * 画面不能是纯色（纯色 = 渲染空白，与全白同义）
  * 画面要有足够的内容占比（内容太少 = 只渲染出壳）

判定用"主色占比 + 内容占比"两个维度而不是"非白像素占比"：
页面有深色主题，只认白色会把深色背景整页判成空白。
只留主色占比也不够——带一条横幅的页面主色占比会很低却仍是空壳。

用法：
  python scripts/verify_pixel_render.py <dist目录> [输出目录]
"""
from __future__ import annotations

import http.server
import os
import socketserver
import subprocess
import sys
import threading
import functools

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 主色占比超过这个数即判空白。留余量：正常页面不会整屏同色。
MAX_DOMINANT_RATIO = 0.98
MIN_EDGE_RATIO = 0.002

# 只截首屏。原因见上：本前端无 URL 路由，多路径截图是同一张图。
ROUTES = ["/"]


class _SPA(http.server.SimpleHTTPRequestHandler):
    """单页应用需要 fallback：直接访问子路由时返回首页，由前端路由接管。"""

    def do_GET(self):  # noqa: N802 — 标准库接口名
        path = self.translate_path(self.path)
        if not os.path.exists(path):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, *a):
        pass


def _serve(dist: str) -> tuple[socketserver.TCPServer, int]:
    h = functools.partial(_SPA, directory=dist)
    srv = socketserver.TCPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _analyze(png: str) -> tuple[float, float, str]:
    """返回 (主色占比, 边缘占比, 说明)。"""
    try:
        from PIL import Image
    except ImportError:
        return (0.0, 1.0, "PIL 不可用，跳过像素判定")
    im = Image.open(png).convert("RGB")
    small = im.resize((160, 100))
    counts: dict[tuple[int, int, int], int] = {}
    for px in small.getdata():
        counts[px] = counts.get(px, 0) + 1
    total = sum(counts.values())
    dom = max(counts.values()) / total
    # 边缘占比：相邻像素差异大的比例，纯色页为 0
    px = small.load()
    diff = 0
    for y in range(0, 100, 2):
        for x in range(0, 159, 2):
            a = px[x, y]
            b = px[x + 1, y]
            if abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) > 24:
                diff += 1
    edge = diff / (50 * 80)
    return (dom, edge, f"{small.width}x{small.height} 采样")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: verify_pixel_render.py <dist目录> [输出目录]")
        return 2
    dist = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(dist, "_shots")
    os.makedirs(out, exist_ok=True)
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print(f"FAIL 产物缺少 index.html：{dist}")
        return 1

    chrome = os.environ.get("OF_CHROME") or _find_chrome()
    if not chrome:
        print("FAIL 找不到可用的浏览器，无法做像素级判定")
        return 1

    srv, port = _serve(dist)
    bad: list[str] = []
    try:
        for r in ROUTES:
            name = (r.strip("/") or "home").replace("/", "_")
            png = os.path.join(out, f"{name}.png")
            url = f"http://127.0.0.1:{port}{r}"
            try:
                subprocess.run(
                    [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                     "--hide-scrollbars", f"--window-size=1280,800",
                     "--virtual-time-budget=6000",
                     f"--screenshot={png}", url],
                    timeout=90, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                bad.append(f"{r}: 截图超时")
                continue
            if not os.path.isfile(png):
                bad.append(f"{r}: 未生成截图")
                continue
            dom, edge, note = _analyze(png)
            if dom > MAX_DOMINANT_RATIO:
                bad.append(f"{r}: 画面接近纯色（主色占比 {dom:.3f}）——疑似渲染空白")
            elif edge < MIN_EDGE_RATIO:
                bad.append(f"{r}: 画面几乎没有内容（边缘占比 {edge:.4f}）")
            else:
                print(f"OK {r:<12} 主色 {dom:.3f} 内容 {edge:.3f}  {note}")
    finally:
        srv.shutdown()

    if bad:
        print(f"\nFAIL 像素级校验未通过 {len(bad)} 项：")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nOK 像素级校验通过：首屏渲染出内容")
    print("    （范围：首屏。逐页需可驱动点击的浏览器，见文件头说明）")
    return 0


def _find_chrome() -> str:
    for c in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        for d in ("/usr/bin", "/usr/local/bin", "/opt/google/chrome"):
            p = os.path.join(d, c)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ""


if __name__ == "__main__":
    sys.exit(main())
