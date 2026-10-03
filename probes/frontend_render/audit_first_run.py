#!/usr/bin/env python3
"""首次使用审计：全新数据目录、空数据，逐页走一遍。

既有审计都先造数据再验，覆盖的是"有内容时能不能用"。而新用户打开
应用的第一分钟面对的恰恰是空状态：空列表、空下拉、空图表。这类界面
只在数据为空时才会走到，有数据时永远碰不到。

判据：真浏览器逐页导航，检查每页是否停在空白或错误态、空态文案是否
自相矛盾（既说"暂无"又显示条目）、英文技术串是否外泄。

用法：python3 probes/frontend_render/audit_first_run.py
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

import audit_interaction as ai

PAGES = ["蒸馏工坊", "对话", "知识库", "待办", "技能与人设",
         "竞技场", "基因组", "运行记录", "用量", "设置"]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def main():
    ai.BASE = os.environ.get("FE_BASE", "http://127.0.0.1:8091/")
    ai.BE_PORT = os.environ.get("BE_PORT", "8788")
    home = os.environ.get("OMEGAFORGE_HOME", "/tmp/first_run_home")
    ai.DATA_HOME = home
    # 全新用户：清掉上一版的数据，确保每页都是真空状态
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)

    me = os.getpid()
    for pid in [int(x) for x in os.listdir("/proc") if x.isdigit()]:
        if pid == me:
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
        except Exception:
            continue
        if "omegaforge.server" in cmd and str(ai.BE_PORT) not in cmd:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
    time.sleep(1.5)

    env = dict(os.environ, OMEGAFORGE_HOME=home)
    static = subprocess.Popen([sys.executable, "-m", "http.server", "8091",
                               "--directory", ai.DIST],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = subprocess.Popen([sys.executable, "-m", "omegaforge.server",
                                "--port", ai.BE_PORT],
                               cwd=os.path.dirname(os.path.dirname(_HERE)), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(ai.BASE, timeout=2).read()
            urllib.request.urlopen(f"http://127.0.0.1:{ai.BE_PORT}/api/runs", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        static.terminate(); backend.terminate()
        raise SystemExit("静态服务或后端未就绪")

    # 确认后端确实是空的，否则这一轮验的就不是空状态。
    # 取值前先确认接口真的应答了：请求失败时 api() 返回的是带 __error__ 的
    # 字典，取 .get("runs") 同样是空——"接口没应答"会和"确实是空的"混为一谈，
    # 于是整个审计会带着一个从未验证过的前提往下跑。
    resp = {
        "运行": ai.api("/api/runs"),
        "对话": ai.api("/api/conversations"),
        "知识库": ai.api("/api/kb/list"),
    }
    print("=== 后端接口可访问 ===")
    for k, r in resp.items():
        check(f"后端 {k} 接口应答正常（否则无从判断空状态）",
              "__error__" not in r, str(r)[:120])

    empty = {
        "运行": not (resp["运行"].get("runs") or []),
        "对话": not (resp["对话"].get("conversations") or []),
        "知识库": (resp["知识库"].get("total") or 0) == 0,
    }
    print("=== 数据目录确认为空 ===")
    for k, v in empty.items():
        check(f"后端 {k} 列表为空（否则验的不是首启状态）", v)

    profile = "/tmp/chrome_profile_firstrun"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [ai.CHROME, "--headless=new", "--remote-debugging-port=9229", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars",
         "--window-size=1440,900", f"--user-data-dir={profile}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ai.wait_devtools(9229)
        targets = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:9229/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        cdp = ai.CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=1440, height=900, deviceScaleFactor=1, mobile=False)

        for label in PAGES:
            print(f"\n=== {label}（空数据）===")
            cdp.send("Page.navigate", url=ai.BASE)
            time.sleep(2.0)
            if label != "蒸馏工坊":
                ok, why = ai.real_click(cdp, ai.nav_btn(label), f"导航{label}")
                check(f"{label}：导航可真实点击", ok, why)
                time.sleep(1.5)
            txt = ai.body_text(cdp)
            body = txt[len("Ω\nOmegaForge\nStudio\n"):]
            # 空态不是错误态：必须给出说明，而不是一片空白或报错
            check(f"{label}：有可见内容（非空白）", len(txt.strip()) > 120,
                  f"页面文本长度 {len(txt)}")
            bad = [w for w in ("Traceback", "undefined", "NaN", "NoneType",
                               "Error:", "Failed to fetch") if w in txt]
            check(f"{label}：不出现英文技术串", not bad, f"出现 {bad}")
            check(f"{label}：不出现内部字段名",
                  not [w for w in ("mock_mode", "base_url", "has_key") if w in txt],
                  f"页面片段：{txt[200:340]!r}")
            # 空态文案自相矛盾：既说"暂无/共 0 条"，又渲染出条目
            says_empty = ("暂无" in txt or "共 0 条" in txt or "还没有" in txt
                          or "无可用" in txt or "还没有解析结果" in txt)
            has_item = ("· 2026/" in txt or "/2026" in txt)
            check(f"{label}：空态文案不自相矛盾",
                  not (says_empty and has_item),
                  f"既说空又列出条目：{txt[200:400]!r}")

        print("\n=== 汇总 ===")
        passed = sum(1 for _, ok in RESULTS if ok)
        print(f"  {passed}/{len(RESULTS)} 通过")
        for name, ok in RESULTS:
            if not ok:
                print(f"  未通过：{name}")
        return 0 if passed == len(RESULTS) else 1
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
