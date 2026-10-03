#!/usr/bin/env python3
"""量一量"只渲染出外壳"时页面文本有多少字符。

首启用 len(body.innerText) > 120 判定"有可见内容"。这个阈值能否区分
"整页没渲染"和"外壳渲染了、内容区是空的"，取决于外壳自身有多少字。
本探针在真浏览器里把内容区隐藏掉，量出外壳文本的字符数。

用法：python3 probes/probe_shell_text_len.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "frontend_render"))

import audit_interaction as ai  # noqa: E402

PAGES = ["蒸馏工坊", "对话", "知识库", "待办", "技能与人设",
         "竞技场", "基因组", "运行记录", "用量", "设置"]


def main():
    home = "/tmp/probe_shell_home"
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, OMEGAFORGE_HOME=home)
    static = subprocess.Popen([sys.executable, "-m", "http.server", "8093",
                               "--directory", ai.DIST],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = subprocess.Popen([sys.executable, "-m", "omegaforge.server",
                                "--port", "8793"],
                               cwd=os.path.dirname(_HERE), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:8093/", timeout=2).read()
            urllib.request.urlopen("http://127.0.0.1:8793/api/runs", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        static.terminate(); backend.terminate()
        raise SystemExit("服务未就绪")

    profile = "/tmp/chrome_profile_shell"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [ai.CHROME, "--headless=new", "--remote-debugging-port=9231", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars",
         "--window-size=1440,900", f"--user-data-dir={profile}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ai.wait_devtools(9231)
        targets = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:9231/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        cdp = ai.CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=1440, height=900, deviceScaleFactor=1, mobile=False)

        print(f"{'页面':<10} {'整页字符':>8} {'隐藏内容区后':>12} {'内容区字符':>10}")
        for label in PAGES:
            cdp.send("Page.navigate", url="http://127.0.0.1:8093/")
            time.sleep(2.0)
            if label != "蒸馏工坊":
                ai.real_click(cdp, ai.nav_btn(label), f"导航{label}")
                time.sleep(1.5)
            full = ai.body_text(cdp)
            # 隐藏内容区，只留外壳（侧栏 + 顶栏 + 页脚）
            ai.js(cdp, """
            (() => {
              const main = document.querySelector('main') ||
                           document.querySelector('[role="main"]');
              if (main) { main.style.display = 'none'; return 'main'; }
              return 'no-main';
            })()""")
            time.sleep(0.4)
            shell = ai.body_text(cdp)
            print(f"{label:<10} {len(full.strip()):>8} {len(shell.strip()):>12} "
                  f"{len(full.strip()) - len(shell.strip()):>10}")
            cdp.send("Page.navigate", url="http://127.0.0.1:8093/")
            time.sleep(1.2)
        return 0
    finally:
        for pr in (proc, static, backend):
            try:
                pr.terminate(); pr.wait(timeout=8)
            except Exception:
                try:
                    pr.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
