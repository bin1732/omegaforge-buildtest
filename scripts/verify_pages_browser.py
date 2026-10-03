#!/usr/bin/env python3
"""逐页真机核查：真实浏览器 + 真实前端产物 + 真实后端。

界面没有 URL 路由，页面只能靠点击侧边导航切换。只读首屏的核查覆盖不到
其余九页——页面在渲染时抛错会表现为空内容，而首屏始终正常，于是"首屏
能画出来"会被误读成"十个页面都好"。

本机与 CI 的差异：CI 的 Windows 装机实例能点开应用，但拿不到逐页证据；
本脚本在沙盒内补上这一层，两者合起来才覆盖"装得上"与"点得开"。

用法: python3 scripts/verify_pages_browser.py [--dist DIR] [--port PORT]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DIST = "/data/workspace/fe_env/dist"
# 前端构建环境是主仓库之外的一份独立拷贝，仓库里没有任何同步步骤。
# 两处源码一旦漂移，这里验到的就是旧代码的产物，而结论照报"通过"。
DEFAULT_BUILD_ROOT = "/data/workspace/fe_env"
DEFAULT_SRC = os.path.join(ROOT, "frontend", "src")
FINGERPRINT_NAME = "dist_src.fingerprint"
DRIVER = os.path.join(HERE, "browser_pages.js")
sys.path.insert(0, HERE)
from page_render_checks import evaluate  # noqa: E402
# 不写死路径：CI 的 runner 是 Windows，'/usr/local/bin/chromium' 在那里不存在，
# 于是下面那句"沙盒内找不到浏览器"会在每次构建上必然触发，报的还是一句
# 把排查方向带去"去装浏览器"的话——真因是路径硬编码，不是缺浏览器。
# 未显式指定时不要求文件存在：浏览器由 playwright 自己管理，找不到时
# node 侧会抛出真实原因，本脚本负责把它原样带出来。
CHROMIUM = os.environ.get("OF_CHROMIUM", "")

EXPECTED_PAGES = [
    "蒸馏工坊", "对话", "知识库", "待办", "技能与人设",
    "竞技场", "基因组", "运行记录", "用量", "设置",
]


def _tree_fingerprint(src: str) -> str:
    """源码树指纹：路径 + 内容。产物与源码是否对应，只能靠它判定。"""
    import hashlib
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d != "node_modules")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src)
            h.update(rel.encode("utf-8", "replace"))
            try:
                with open(full, "rb") as f:
                    h.update(hashlib.sha256(f.read()).hexdigest().encode())
            except OSError:
                h.update(b"<unreadable>")
    return h.hexdigest()


def _sync_src(src: str, build_root: str) -> int:
    """把主仓库前端源码同步进构建环境。不同步就构建，等于验旧代码。"""
    dst = os.path.join(build_root, "src")
    if not os.path.isdir(src):
        print(f"FAIL: 找不到前端源码 {src}", file=sys.stderr)
        return 1
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"已同步前端源码 → {dst}")
    return 0


def verify_bundle_source(dist: str, src: str):
    """判定产物是否由当前源码构建。

    单独成函数，是为了让守卫能按"理由"断言而不是按退出码：main() 在指纹
    判定之后还会因为后端未就绪、浏览器拿不到页面等原因返回 1，按退出码
    断言的用例在指纹判定被撤掉时照样通过——恒真的守卫比没有更坏。
    """
    fp_path = os.path.join(dist, FINGERPRINT_NAME)
    if not os.path.isfile(fp_path):
        return False, (f"产物缺少源码指纹 {fp_path} —— "
                       "无法确认验的是哪份源码")
    with open(fp_path) as f:
        recorded = f.read().strip()
    current = _tree_fingerprint(src)
    if recorded != current:
        return False, ("前端产物与当前源码不一致 —— 请先 --sync 后重建前端"
                       f"（产物 {recorded[:12]} / 源码 {current[:12]}）")
    return True, ""


def _wait_backend(base: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/status", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _port_open(port: int) -> bool:
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        s.close()
    return False


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


def _make_proxy(dist: str, backend: str):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=dist, **kw)

        def log_message(self, *a):
            pass

        def _proxy(self, method: str):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(
                backend + self.path, data=body, method=method)
            ct = self.headers.get("Content-Type")
            if ct:
                req.add_header("Content-Type", ct)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                    self.send_response(r.status)
                    ctype = r.headers.get("Content-Type")
                    if ctype:
                        self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/"):
                self._proxy("GET")
            else:
                super().do_GET()

        def do_POST(self):
            self._proxy("POST")

        def do_DELETE(self):
            self._proxy("DELETE")

        def do_PUT(self):
            self._proxy("PUT")

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=DEFAULT_DIST)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--backend-port", type=int, default=8787)
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--build-root", default=DEFAULT_BUILD_ROOT)
    ap.add_argument("--sync", action="store_true",
                    help="先把主仓库前端源码同步进构建环境")
    ap.add_argument("--record-fingerprint", action="store_true",
                    help="构建完成后写入源码指纹，供后续核对产物来源")
    ap.add_argument("--shots", default="/tmp/pageshot",
                    help="逐页截图输出目录（供像素级校验复用）")
    args = ap.parse_args(argv)

    if args.sync:
        rc = _sync_src(args.src, args.build_root)
        if rc:
            return rc

    if not os.path.isfile(os.path.join(args.dist, "index.html")):
        print("FAIL: 前端产物缺失 index.html —— 请先构建前端", file=sys.stderr)
        return 1

    if args.record_fingerprint:
        with open(os.path.join(args.dist, FINGERPRINT_NAME), "w") as f:
            f.write(_tree_fingerprint(args.src) + "\n")
        print(f"已记录源码指纹 → {os.path.join(args.dist, FINGERPRINT_NAME)}")
        return 0

    # 产物与源码不对应时，验到的是另一份代码的界面：结论会挂在过期产物上。
    # 指纹缺失同样不能放行——放行等于这一层永远不生效。
    ok, reason = verify_bundle_source(args.dist, args.src)
    if not ok:
        print("FAIL: " + reason, file=sys.stderr)
        return 1
    shots_dir = args.shots
    os.makedirs(shots_dir, exist_ok=True)

    # 只在显式指定了浏览器路径时才检查它存在；未指定时交给 playwright 定位，
    # 那里的报错会指明真实原因（模块找不到 / 浏览器未安装）。
    if CHROMIUM and not os.path.isfile(CHROMIUM):
        print("FAIL: 指定的浏览器不存在：%s" % CHROMIUM, file=sys.stderr)
        return 1

    # 前端把后端地址写死在 8787，核查必须落在同一端口，
    # 否则每一页都会连接被拒——那时验到的是环境，不是界面。
    backend_port = args.backend_port
    # 上一次运行刚结束时不重试会立刻判"端口被占用"，而真实原因是上一个
    # 进程还没退干净——这条报错会把排查带向"环境里别的东西占了端口"。
    deadline = time.time() + 15
    while backend_port and _port_open(backend_port) and time.time() < deadline:
        time.sleep(0.5)
    if backend_port and _port_open(backend_port):
        print("FAIL: 端口 %d 已被占用" % backend_port, file=sys.stderr)
        return 1
    backend = f"http://127.0.0.1:{backend_port}"
    env = dict(os.environ)
    # 环境变量名必须是 OMEGAFORGE_HOME（paths.py 里 _ENV 的值）。
    # 写成 OF_HOME 不会报错，只是完全不生效——数据仍落到 cwd，
    # 界面上看到的是别处的数据，而症状表现为"刚写的东西读不回来"。
    env["OMEGAFORGE_HOME"] = os.path.join(ROOT, ".ofpages")
    proc = subprocess.Popen(
        [sys.executable, "-m", "omegaforge.server", "--port", str(backend_port)],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_backend(backend):
            print("FAIL: 后端未就绪", file=sys.stderr)
            return 1

        front_port = args.port or _free_port()
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", front_port), _make_proxy(args.dist, backend))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        out = subprocess.run(
            ["node", DRIVER, f"http://127.0.0.1:{front_port}/",
             shots_dir],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    if out.returncode != 0:
        print("FAIL: 浏览器驱动异常 —— " + (out.stderr or "").strip()[:400])
        return 1

    events = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except Exception:
                pass

    pages = [e for e in events if e.get("event") == "page"]
    nav = next((e for e in events if e.get("event") == "nav"), None)
    fails = evaluate(events)

    print(f"页面 {len(pages)} 个，导航 {len((nav or {}).get('labels') or [])} 项")
    for p in pages:
        # 打不开的页没有 nodes/textLen：None 直接进格式串会抛出 TypeError，
        # 把"某页打不开"变成"脚本崩了"，诊断随之丢失。
        nodes = p.get("nodes")
        text_len = p.get("textLen")
        print(f"  {str(p.get('label')):<8} "
              f"节点={'缺' if nodes is None else nodes:<4} "
              f"文案={'缺' if text_len is None else text_len:<5} "
              f"报错={len(p.get('errors') or [])}"
              + (f" 打不开={p.get('error')}" if p.get("error") else ""))
        for err in p.get('errors') or []:
            print(f"      · {err[:220]}")
    if fails:
        print("\n=== 逐页核查未通过 ===")
        for f in fails:
            print("  ❌ " + f)
        return 1
    print("\n=== 逐页核查通过：十页均能在真实浏览器中打开并渲染 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
