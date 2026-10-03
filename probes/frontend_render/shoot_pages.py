#!/usr/bin/env python3
"""用真实无头浏览器渲染界面并截图。

## 为什么需要它

jsdom 不做布局与绘制，只能验文本契约。原先多轮把"像素级视觉"列为无法
验证，依据是"沙盒没有浏览器"——该依据不成立：系统里一直有 Chrome。
本脚本用 CDP 驱动真浏览器，补上这一层。

## 覆盖范围与边界

能做：真实布局、样式是否生效、元素是否重叠溢出、暗色模式、字体渲染。
做不到：真实交互手感、动画时序、桌面外壳内的表现。

用法：python3 probes/shoot_pages.py [输出目录]
前置：后端在 8787，dist 静态服务在 8080。
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket  # websocket-client

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
PORT = 9222
BASE = os.environ.get("FE_BASE", "http://127.0.0.1:8080/")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots"

# 导航项：与 App.tsx 的 NAV 一致。点击后再截图。
NAV = [
    ("forge", "蒸馏工坊"),
    ("chat", "对话"),
    ("knowledge", "知识库"),
    ("tasks", "待办"),
    ("skills", "技能与人设"),
    ("arena", "竞技场"),
    ("genome", "基因组"),
    ("runs", "运行记录"),
    ("usage", "用量"),
    ("settings", "设置"),
]


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self._id = 0

    def send(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_devtools(port, timeout=40):
    url = f"http://127.0.0.1:{port}/json/version"
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(0.5)
    raise SystemExit("浏览器调试端口未就绪")


def _serve():
    """拉起静态服务与后端。

    必须由本脚本托管生命周期：在命令里用后台进程启动会挂住 stdout，
    且进程会随会话结束被回收，导致浏览器访问时连接被拒。
    """
    static = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8080",
         "--directory", os.environ.get("FE_DIST", "/data/workspace/fe_env/dist")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend_port = os.environ.get("BE_PORT", "8787")
    backend = subprocess.Popen(
        [sys.executable, "-m", "omegaforge.server", "--port", backend_port],
        cwd=os.environ.get("BE_CWD", "/data/workspace/LATEST"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/", timeout=2).read()
            urllib.request.urlopen(
                f"http://127.0.0.1:{backend_port}/api/runs", timeout=2).read()
            return static, backend
        except Exception:
            time.sleep(0.5)
    static.terminate()
    backend.terminate()
    raise SystemExit("静态服务或后端未就绪")


def main():
    if not CHROME:
        raise SystemExit("找不到浏览器")
    os.makedirs(OUT, exist_ok=True)
    static, backend = _serve()

    profile = "/tmp/chrome_profile_shoot"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         "--window-size=1440,900", f"--user-data-dir={profile}",
         "--disable-lcd-text", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ver = wait_devtools(PORT)
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=1440, height=900, deviceScaleFactor=1, mobile=False)

        results = []
        for nav_id, label in NAV:
            cdp.send("Page.navigate", url=BASE)
            time.sleep(2.5)
            # 点击对应的导航项（App.tsx 用 state 切换，不是路由）
            js = """
            (() => {
              const els = Array.from(document.querySelectorAll('button,[role="button"],a'));
              const t = els.find(e => (e.textContent||'').includes(%s));
              if (t) { t.click(); return 'clicked'; }
              return 'notfound';
            })()
            """ % json.dumps(label)
            r = cdp.send("Runtime.evaluate", expression=js,
                         returnByValue=True, awaitPromise=False)
            clicked = (r.get("result") or {}).get("value")
            time.sleep(2.5)
            shot = cdp.send("Page.captureScreenshot", format="png")
            data = base64.b64decode(shot["data"])
            fp = os.path.join(OUT, f"{nav_id}.png")
            with open(fp, "wb") as f:
                f.write(data)
            # 同时取一份可见文本，便于与截图对照
            txt = cdp.send("Runtime.evaluate",
                           expression="document.body.innerText.slice(0, 1200)",
                           returnByValue=True)
            results.append((nav_id, label, clicked, len(data),
                            (txt.get("result") or {}).get("value", "")))
            print(f"  [{nav_id}] 点击={clicked} 字节={len(data)}")

        with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": a, "label": b, "clicked": c, "bytes": d, "text": e}
                       for a, b, c, d, e in results], f, ensure_ascii=False, indent=1)
        print(f"\n截图输出：{OUT}")
        return 0
    finally:
        for pr in (proc, static, backend):
            try:
                pr.terminate()
                pr.wait(timeout=8)
            except Exception:
                try:
                    pr.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
