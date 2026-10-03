#!/usr/bin/env python3
"""真浏览器布局审计：溢出、零尺寸、对比度、窄屏。

jsdom 不做布局与绘制，这些只能由真浏览器给出。判据全部来自
getBoundingClientRect 与 getComputedStyle 的真实返回值，不靠推断。

输出：JSON + 控制台摘要。
用法：python3 probes/audit_layout.py [输出目录]
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from shoot_pages import CDP, NAV, PORT, _serve, wait_devtools  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/layout_audit"

# 单页扫描到的元素数低于此值，说明页面未真正加载（空白、脚本报错、
# 路由未渲染）。此时四类问题都会是空列表，若不单独判定，会被当成
# "这一页很整洁"——失效与整洁将无法区分。
MIN_SCANNED = int(os.environ.get("LAYOUT_MIN_SCANNED", "15"))

# 三种视口：宽屏、笔记本、窄屏
VIEWPORTS = [(1440, 900, "宽屏"), (1280, 800, "笔记本"), (390, 844, "窄屏")]

# 审计脚本：只报客观可判定的问题，不做审美评分
AUDIT_JS = r"""
(() => {
  const vw = window.innerWidth;
  const result = { overflow: [], zero: [], lowContrast: [], tiny: [] };

  // 相对亮度（WCAG）
  const lum = (c) => {
    const [r, g, b] = c;
    const f = (v) => {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const parse = (s) => {
    const m = s.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x.trim()));
    return { rgb: [p[0], p[1], p[2]], a: p.length > 3 ? p[3] : 1 };
  };

  // display:none 只作用于元素自身，子元素的 computed display 仍是 block，
  // 但 rect 恒为 0——只看自身会把"父级有意隐藏"误判成"塌陷成零尺寸"。
  // 实测：品牌文字在窄屏被 hidden 隐藏后，审计误报 20 处。
  const hiddenByAncestor = (el) => {
    let p = el;
    while (p && p !== document.documentElement) {
      const s = getComputedStyle(p);
      if (s.display === 'none' || s.visibility === 'hidden') return true;
      p = p.parentElement;
    }
    return false;
  };

  const els = Array.from(document.querySelectorAll('body *'));
  for (const el of els) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (hiddenByAncestor(el)) continue;
    const r = el.getBoundingClientRect();

    // 1) 横向溢出视口
    if (r.width > 0 && r.right > vw + 1) {
      result.overflow.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || '').toString().slice(0, 60),
        right: Math.round(r.right), vw,
        text: (el.textContent || '').trim().slice(0, 40),
      });
    }

    // 2) 应当可见却零尺寸（有文本却被压成 0 高）
    const txt = (el.textContent || '').trim();
    if (txt && el.children.length === 0 && (r.width < 1 || r.height < 1)) {
      result.zero.push({
        tag: el.tagName.toLowerCase(),
        text: txt.slice(0, 40),
        w: Math.round(r.width), h: Math.round(r.height),
      });
    }

    // 3) 文本对比度（仅叶子文本节点，取父链上的第一个不透明背景）
    if (txt && el.children.length === 0 && r.width > 0 && r.height > 0) {
      const fg = parse(cs.color);
      if (fg && fg.a > 0.5) {
        let bg = null, p = el;
        while (p && p !== document.documentElement) {
          const b = parse(getComputedStyle(p).backgroundColor);
          if (b && b.a > 0.5) { bg = b; break; }
          p = p.parentElement;
        }
        if (!bg) bg = { rgb: [255, 255, 255], a: 1 };
        const L1 = lum(fg.rgb), L2 = lum(bg.rgb);
        const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
        const size = parseFloat(cs.fontSize);
        const bold = parseInt(cs.fontWeight, 10) >= 700;
        // WCAG AA：普通文本 4.5，大号（>=18.66px 或 >=14px 粗体）3.0
        const need = (size >= 18.66 || (bold && size >= 14)) ? 3.0 : 4.5;
        if (ratio < need) {
          result.lowContrast.push({
            text: txt.slice(0, 40),
            ratio: Math.round(ratio * 100) / 100, need,
            color: cs.color, bg: `rgb(${bg.rgb.join(',')})`,
            size: Math.round(size * 10) / 10,
          });
        }
      }
    }

    // 4) 可点击目标过小（< 24x24，触控可用性下限）
    const clickable = el.tagName === 'BUTTON' || el.tagName === 'A' ||
                      cs.cursor === 'pointer';
    if (clickable && r.width > 0 && r.height > 0 && (r.width < 24 || r.height < 24)) {
      result.tiny.push({
        tag: el.tagName.toLowerCase(),
        w: Math.round(r.width), h: Math.round(r.height),
        text: (el.textContent || '').trim().slice(0, 30),
      });
    }
  }
  // 同一问题可能重复出现，按特征去重
  for (const k of Object.keys(result)) {
    const seen = new Set();
    result[k] = result[k].filter((o) => {
      const key = JSON.stringify(o);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }
  result.scanned = els.length;
  return result;
})()
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    static, backend = _serve()
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    profile = "/tmp/chrome_profile_audit"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [chrome, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--hide-scrollbars", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = {}
    blind = []
    try:
        wait_devtools(PORT)
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        for w, h, vname in VIEWPORTS:
            cdp.send("Emulation.setDeviceMetricsOverride",
                     width=w, height=h, deviceScaleFactor=1, mobile=False)
            for nav_id, label in NAV:
                cdp.send("Page.navigate",
                         url=os.environ.get("FE_BASE", "http://127.0.0.1:8080/"))
                time.sleep(2.2)
                cdp.send("Runtime.evaluate", expression=(
                    "(() => { const els = Array.from("
                    "document.querySelectorAll('button,[role=\"button\"],a'));"
                    " const t = els.find(e => (e.textContent||'').includes(%s));"
                    " if (t) { t.click(); return 'clicked'; } return 'notfound'; })()"
                    % json.dumps(label)), returnByValue=True)
                time.sleep(2.0)
                r = cdp.send("Runtime.evaluate", expression=AUDIT_JS,
                             returnByValue=True)
                data = (r.get("result") or {}).get("value") or {}
                key = f"{vname}/{nav_id}"
                report[key] = data
                scanned = int(data.get("scanned") or 0)
                issues = {k: v for k, v in data.items()
                          if isinstance(v, list)}
                total = sum(len(v) for v in issues.values())
                if scanned < MIN_SCANNED:
                    blind.append(key)
                    flag = f"未真正加载（仅 {scanned} 个元素）"
                elif total:
                    flag = f"{total} 处"
                else:
                    flag = "OK"
                print(f"  [{vname}] {label}: {flag}")
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

    fp = os.path.join(OUT, "report.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # 汇总：按问题类型统计
    agg = {}
    for key, data in report.items():
        for kind, items in data.items():
            if not isinstance(items, list):
                continue
            agg.setdefault(kind, []).extend(
                [(key, i) for i in items])
    print("\n=== 汇总 ===")
    for kind in ("overflow", "zero", "lowContrast", "tiny"):
        items = agg.get(kind, [])
        print(f"  {kind}: {len(items)} 处")
    if blind:
        print(f"  未真正加载: {len(blind)} 页")
        for key in blind[:5]:
            print(f"    - {key}")
    print(f"\n报告：{fp}")
    total_issues = sum(len(v) for v in agg.values())
    if total_issues or blind:
        print("判定：未通过")
        return 1
    print("判定：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
