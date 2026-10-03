#!/usr/bin/env python3
"""后端未启动时，界面提示必须可行动。

背景：后端没起来时，请求层的 fetch 直接抛网络异常，既没有 HTTP 状态也
没有后端错误码，文案层只能落到兜底"操作失败，请稍后重试"。这句对使用者
是误导——重试不会有帮助，真正的原因是本机服务没启动。

判据用真浏览器检验：故意不起后端，只看界面上真实渲染出来的文字。
仅断言源码里出现某句文案属于字面量检查，改了文案或没接通都抓不到。

用法：python3 probes/frontend_render/audit_backend_down.py
退出码：0=全过，1=有失败。
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
sys.path.insert(0, _HERE)

import websocket

DIST = os.environ.get("FE_DIST", "/data/workspace/fe_env/dist")
PORT = int(os.environ.get("CDP_PORT", "9228"))
HTTP_PORT = os.environ.get("FE_HTTP_PORT", "8098")
# 前端产物里写死的地址：本审计要验的正是"它连不上"这一情形
BE_URL = os.environ.get("FE_BE_URL", "http://127.0.0.1:8787/")
CHROME = shutil.which("google-chrome") or shutil.which("chromium")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def scene_holds():
    """确认"后端没起来"这一场景真的成立。

    本审计验的是后端不可达时的界面文案，前提是那个地址上确实没有服务。
    若别的进程占着它（跑别的审计时留下的后端就会这样），页面会正常渲染，
    于是"界面说明连不上本机服务"这类断言一律失败——报的是产品缺陷，实际
    是场景没搭起来。

    更麻烦的是它并不总是这样：随便起一个答非所问的服务时页面同样显示
    连不上，断言又会全过。同一个前提缺失，两种相反的结果，排查时无从下手。

    因此场景不成立时直接终止，并给出与产品失败可区分的说明。
    """
    try:
        urllib.request.urlopen(BE_URL + "api/runs", timeout=3).read()
    except Exception:
        return True, ""
    return False, (f"后端地址 {BE_URL} 上已有服务在响应，"
                   f"“后端未启动”这一场景不成立")


def main():
    if not CHROME:
        raise SystemExit("找不到浏览器")
    ok_scene, why = scene_holds()
    if not ok_scene:
        print(f"场景不成立：{why}")
        print("请先结束占用该地址的服务，再运行本审计")
        return 1
    static = subprocess.Popen([sys.executable, "-m", "http.server", HTTP_PORT,
                               "--directory", DIST],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        static.terminate()
        raise SystemExit("静态服务未就绪")

    profile = "/tmp/chrome_profile_down"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars",
         "--window-size=1440,900", f"--user-data-dir={profile}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        i = 0

        def send(method, **params):
            nonlocal i
            i += 1
            ws.send(json.dumps({"id": i, "method": method, "params": params}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == i:
                    if "error" in r:
                        raise RuntimeError(f"{method}: {r['error']}")
                    return r.get("result", {})

        send("Page.enable")
        send("Runtime.enable")
        # 后端不起：正是本检查脚本要验的场景
        send("Page.navigate", url=f"http://127.0.0.1:{HTTP_PORT}/")
        time.sleep(6.0)
        txt = send("Runtime.evaluate", expression="document.body.innerText.slice(0,1500)",
                   returnByValue=True)["result"].get("value", "") or ""

        print("=== 后端未启动时的界面文本 ===")
        print("  " + txt[:300].replace("\n", " | "))
        print()
        check("界面说明连不上本机服务", "无法连接到本机服务" in txt,
              f"页面片段：{txt[:160]!r}")
        check("不出现「请稍后重试」这类重试无效的建议",
              "请稍后重试" not in txt, f"页面片段：{txt[:160]!r}")
        check("顶部状态给出服务未连接", "服务未连接" in txt)
        for eng in ("Failed to fetch", "TypeError", "Traceback", "undefined"):
            check(f"不出现英文技术串 {eng}", eng not in txt)
        try:
            ws.close()
        except Exception:
            pass
    finally:
        proc.terminate()
        static.terminate()

    print("\n=== 汇总 ===")
    bad = [n for n, ok in RESULTS if not ok]
    print(f"  {len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    for n in bad:
        print(f"  未通过：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
