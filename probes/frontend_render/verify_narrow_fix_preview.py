#!/usr/bin/env python3
"""窄屏修复方案预演：在当前产物上模拟改动后的布局，验证根因判断是否成立。

## 为什么需要它

生产构建在本沙盒反复被中断，无法产出含修复的新产物。为避免"改了代码却不
知道有没有用"，这里在浏览器里直接施加与修复等价的 DOM/CSS 改动，再跑同一
份布局审计，对比溢出数。

## 诚实边界

本脚本验证的是**修复方案的有效性**，不是"新产物已通过审计"。二者等价的前
提是：预演施加的改动与源码改动语义一致。因此每处改动都在 APPLY_JS 中与源码
逐条对应标注。

用法：python3 probes/verify_narrow_fix_preview.py [输出目录]
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from shoot_pages import CDP, NAV, PORT, _serve, wait_devtools  # noqa: E402
from audit_layout import AUDIT_JS  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fix_preview"

# 与 App.tsx 源码改动逐条对应：
#   1. aside 宽度 w-60 → w-14（窄屏），中等屏以上保持 w-60
#   2. 导航按钮文本 → 首字（窄屏）
#   3. 品牌文字块隐藏（窄屏）
#   4. 底部状态文字隐藏（窄屏）
#   5. 内容区内边距 p-6 → p-4（窄屏）
APPLY_JS = r"""
(() => {
  const st = document.createElement('style');
  st.textContent = `
    @media (max-width: 767px) {
      aside { width: 3.5rem !important; }
      /* 源码：md:hidden / hidden md:inline */
      aside .leading-tight { display: none !important; }
      aside button { text-align: center !important; padding-left: .5rem !important;
                     padding-right: .5rem !important; }
      aside .text-\\[11px\\] { display: none !important; }
      main .mx-auto.max-w-6xl { padding: 1rem !important; }
    }
  `;
  document.head.appendChild(st);

  // 源码：窄屏只渲染 label.slice(0,1)
  const labels = %s;
  const vw = window.innerWidth;
  if (vw < 768) {
    for (const b of document.querySelectorAll('aside button')) {
      const t = (b.textContent || '').trim();
      if (labels.includes(t)) b.textContent = t.slice(0, 1);
    }
  }
  return 'applied';
})()
""" % json.dumps([lbl for _, lbl in NAV], ensure_ascii=False)


def collect(cdp, width, height, apply_fix):
    out = {}
    for nav_id, label in NAV:
        cdp.send("Page.navigate",
                 url=os.environ.get("FE_BASE", "http://127.0.0.1:8080/"))
        time.sleep(1.8)
        cdp.send("Runtime.evaluate", expression=(
            "(() => { const els = Array.from("
            "document.querySelectorAll('button,[role=\"button\"],a'));"
            " const t = els.find(e => (e.textContent||'').includes(%s));"
            " if (t) { t.click(); return 1; } return 0; })()" % json.dumps(label)),
            returnByValue=True)
        time.sleep(1.5)
        if apply_fix:
            cdp.send("Runtime.evaluate", expression=APPLY_JS, returnByValue=True)
            time.sleep(0.6)
        r = cdp.send("Runtime.evaluate", expression=AUDIT_JS, returnByValue=True)
        out[nav_id] = (r.get("result") or {}).get("value") or {}
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    static, backend = _serve()
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    prof = "/tmp/cp_fixprev"
    shutil.rmtree(prof, ignore_errors=True)
    proc = subprocess.Popen(
        [chrome, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--hide-scrollbars", "--remote-allow-origins=*",
         f"--user-data-dir={prof}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = {}
    try:
        wait_devtools(PORT)
        t = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/list", timeout=5).read().decode())
        pg = [x for x in t if x.get("type") == "page"][0]
        cdp = CDP(pg["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=390, height=844, deviceScaleFactor=1, mobile=False)

        base = collect(cdp, 390, 844, apply_fix=False)
        fixed = collect(cdp, 390, 844, apply_fix=True)
        report = {"修复前": base, "修复后(预演)": fixed}
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

    def total(d):
        return sum(len(v.get("overflow", [])) for v in d.values())

    b, f = total(base) if False else (0, 0)
    b = sum(len(v.get("overflow", [])) for v in report["修复前"].values())
    f = sum(len(v.get("overflow", [])) for v in report["修复后(预演)"].values())

    print("=== 390px 窄屏横向溢出对比 ===")
    for nav_id, label in NAV:
        x = len(report["修复前"].get(nav_id, {}).get("overflow", []))
        y = len(report["修复后(预演)"].get(nav_id, {}).get("overflow", []))
        print(f"  {label:<8} 修复前 {x:>3} → 修复后 {y:>3}")
    print(f"\n  合计 {b} → {f}")

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=1)
    print(f"\n报告：{os.path.join(OUT, 'report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
