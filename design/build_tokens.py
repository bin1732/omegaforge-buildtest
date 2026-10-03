# -*- coding: utf-8 -*-
"""OmegaForge 设计 token 构建器 —— 单一事实源 -> 多平台产物

三层架构（严格单向依赖）：
    primitive(原始值,不可变) -> semantic(语义角色,主题可切换) -> component(组件局部覆盖)

颜色以 OKLCH 通道三元组存储（不是完整 oklch() 函数），
以便 oklch(var(--x) / 0.5) 做原生透明度合成。
"""
import json, os
from palette import RAMPS, SCALE, SURFACE_SCALE, hexes, contrast, rel_lum
from oklch import hex_to_oklch

OUT = os.path.dirname(os.path.abspath(__file__))

# ===== 品牌校验点色：必须精确复现，不经过色域裁剪 =====
ANCHORS = {"brand": {"600": "#7c5cff"}}
for ramp, slots in ANCHORS.items():
    for slot, hx in slots.items():
        L, C, H = hex_to_oklch(hx)
        idx = SCALE.index(slot)
        RAMPS[ramp][idx] = (round(L, 6), round(C, 6), round(H, 4))

def triple(v):
    return f"{v[0]:.4f} {v[1]:.4f} {v[2]:.2f}"

# ---------- primitive ----------
primitive = {
    "color": {},
    "space": {k: f"{v}px" for k, v in {
        "0": "0", "px": "1", "0_5": "2", "1": "4", "1_5": "6", "2": "8",
        "2_5": "10", "3": "12", "3_5": "14", "4": "16", "5": "20", "6": "24",
        "7": "28", "8": "32", "10": "40", "12": "48", "16": "64"}.items()},
    "radius": {"base": "0.625rem"},
    "fontSize": {k: f"{v[0]}px" for k, v in {
        "2xs": "11", "xs": "12", "sm": "13", "base": "14", "md": "16",
        "lg": "18", "xl": "20", "2xl": "24", "3xl": "30", "4xl": "36"}.items()},
    "lineHeight": {"tight": "1.2", "snug": "1.4", "normal": "1.6", "relaxed": "1.75"},
    "duration": {"instant": "80ms", "fast": "120ms", "base": "200ms", "slow": "300ms", "slower": "500ms"},
    "easing": {"standard": "cubic-bezier(.2,0,0,1)",
               "decelerate": "cubic-bezier(.16,1,.3,1)",
               "accelerate": "cubic-bezier(.4,0,1,1)"},
    "z": {"base": "0", "raised": "10", "dropdown": "100", "overlay": "200",
          "modal": "300", "popover": "400", "toast": "500"},
}
for name in RAMPS:
    steps = SURFACE_SCALE if name == "surface" else SCALE
    primitive["color"][name] = {s: triple(v) for s, v in zip(steps, RAMPS[name])}
primitive["color"]["white"] = "1.0000 0.0000 0.00"
primitive["color"]["black"] = "0.0000 0.0000 0.00"

# ---------- semantic：深色（Dark Ops 默认）----------
S = lambda r, s: (f"var(--of-{r}-{s})" if s else f"var(--of-{r})")
DARK = {
    "background":            S("surface", "0"),
    "foreground":            S("surface", "12"),
    "card":                  S("surface", "1"),
    "card-foreground":       S("surface", "12"),
    "popover":               S("surface", "2"),
    "popover-foreground":    S("surface", "12"),
    # primary 承载白字，须达 AA：brand-600(#7c5cff) 白字仅 4.35 不达标，故用 700(#6f3ff9,5.59)
    "primary":               S("brand", "700"),
    "primary-foreground":    S("white", ""),
    # 装饰性品牌强调（logo/发光/边框/图标，不承载正文）——精确的 Ω 紫校验点
    "primary-emphasis":      S("brand", "600"),
    "secondary":             S("surface", "2"),
    "secondary-foreground":  S("surface", "11"),
    "muted":                 S("surface", "2"),
    "muted-foreground":      S("surface", "8"),
    "accent":                S("surface", "3"),
    "accent-foreground":     S("surface", "11"),
    "destructive":           S("danger", "700"),
    "destructive-foreground":S("white", ""),
    "success":               S("success", "700"),
    "success-foreground":    S("white", ""),
    # 橙黄底配白字仅 3.57 不可读，改黑字 5.88
    "warning":               S("warning", "600"),
    "warning-foreground":    S("black", ""),
    "border":                S("surface", "2"),
    "input":                 S("surface", "3"),
    "ring":                  S("brand", "500"),
}
LIGHT = {
    "background":            S("surface", "12"),
    "foreground":            S("surface", "1"),
    "card":                  S("white", ""),
    "card-foreground":       S("surface", "1"),
    "popover":               S("white", ""),
    "popover-foreground":    S("surface", "1"),
    "primary":               S("brand", "700"),
    "primary-foreground":    S("white", ""),
    "primary-emphasis":      S("brand", "600"),
    "secondary":             S("surface", "11"),
    "secondary-foreground":  S("surface", "2"),
    "muted":                 S("surface", "11"),
    "muted-foreground":      S("surface", "6"),
    "accent":                S("surface", "11"),
    "accent-foreground":     S("surface", "2"),
    "destructive":           S("danger", "700"),
    "destructive-foreground":S("white", ""),
    "success":               S("success", "700"),
    "success-foreground":    S("white", ""),
    "warning":               S("warning", "700"),
    "warning-foreground":    S("white", ""),
    "border":                S("surface", "10"),
    "input":                 S("surface", "10"),
    "ring":                  S("brand", "600"),
}
# OmegaForge 业务语义（component 层之上，产品专属）
def business(mode):
    d = mode == "dark"
    return {
        "verdict-win":   S("success", "600" if d else "700"),
        "verdict-tie":   S("warning", "600" if d else "700"),
        "verdict-loss":  S("danger", "600" if d else "700"),
        "gene-persona":  S("brand", "600"),
        "gene-tool":     S("success", "600"),
        "gene-workflow": S("brand", "400"),
        "gene-upgrade":  S("warning", "500"),
        "phase-active":  S("brand", "500"),
        "phase-done":    S("success", "600"),
        "phase-pending": S("surface", "6"),
        "log-bg":        S("surface", "0"),
        "log-fg":        S("surface", "9"),
        "budget-ok":     S("success", "600"),
        "budget-warn":   S("warning", "500"),
        "budget-over":   S("danger", "600"),
    }

# ---------- 生成 CSS ----------
def css_block(sel, mapping, indent="  "):
    lines = [f"{sel} {{"]
    for k, v in mapping.items():
        lines.append(f'{indent}--{k}: {v};')
    lines.append("}")
    return "\n".join(lines)

L = []
L.append("/* ============================================================")
L.append(" * OmegaForge Design Tokens —— 自动生成，请勿手改")
L.append(f" * 源文件: design/tokens.json  生成脚本: design/build_tokens.py")
L.append(" * 颜色模型: OKLCH（感知均匀）。通道三元组存储，支持 oklch(var(--x)/α) 合成")
L.append(" * 三层: primitive -> semantic -> component（单向依赖）")
L.append(" * ============================================================ */")
L.append("")
L.append("/* ---------- L1 primitive：原始值，不可变 ---------- */")
def _prim_colors():
    out = {}
    for r, d in primitive["color"].items():
        if isinstance(d, dict):
            for s, v in d.items():
                out[f"of-{r}-{s}"] = v
        else:
            out[f"of-{r}"] = d      # white / black：无档位后缀，不加横杠
    return out
L.append(css_block(":root", _prim_colors()))
for cat in ("space", "radius", "fontSize", "lineHeight", "duration", "easing", "z"):
    if cat == "radius":
        L.append(css_block(":root", {"of-radius": primitive[cat]["base"]}))
    else:
        L.append(css_block(":root", {f"of-{cat}-{k}": v for k, v in primitive[cat].items()}))
L.append("")
L.append("/* ---------- L2 semantic：深色（Dark Ops 默认）---------- */")
L.append(css_block(':root, [data-theme="dark"]', dict(DARK, **business("dark"))))
L.append("")
L.append("/* ---------- L2 semantic：浅色 ---------- */")
L.append(css_block('[data-theme="light"]', dict(LIGHT, **business("light"))))
L.append("")
L.append("/* ---------- 派生：圆角（从单一 --of-radius 派生）---------- */")
L.append(""":root {
  --radius-sm: calc(var(--of-radius) - 4px);
  --radius-md: calc(var(--of-radius) - 2px);
  --radius-lg: var(--of-radius);
  --radius-xl: calc(var(--of-radius) + 4px);
  --radius-full: 9999px;
}""")
L.append("")
L.append("/* ---------- 派生：阴影（深色模式需内高光，纯黑阴影在深底不可见）---------- */")
L.append(""":root, [data-theme="dark"] {
  --shadow-sm: 0 1px 0 oklch(var(--of-white) / .04) inset, 0 2px 8px oklch(0 0 0 / .35);
  --shadow-md: 0 1px 0 oklch(var(--of-white) / .05) inset, 0 8px 28px oklch(0 0 0 / .45);
  --shadow-lg: 0 1px 0 oklch(var(--of-white) / .06) inset, 0 16px 48px oklch(0 0 0 / .55);
  --shadow-glow: 0 4px 24px oklch(var(--of-brand-600) / .35);
  --ring-focus: 0 0 0 2px var(--background), 0 0 0 4px var(--ring);
}
[data-theme="light"] {
  --shadow-sm: 0 1px 2px oklch(0 0 0 / .06);
  --shadow-md: 0 4px 16px oklch(0 0 0 / .08);
  --shadow-lg: 0 12px 32px oklch(0 0 0 / .12);
  --shadow-glow: 0 4px 20px oklch(var(--of-brand-600) / .22);
  --ring-focus: 0 0 0 2px var(--background), 0 0 0 4px var(--ring);
}""")
css = "\n".join(L) + "\n"

tokens_json = {
    "$schema": "omegaforge/design-tokens/v1",
    "comment": "单一事实源。改动此文件后运行 build_tokens.py 重新生成 CSS。",
    "primitive": primitive,
    "semantic": {"dark": dict(DARK, **business("dark")),
                 "light": dict(LIGHT, **business("light"))},
}

# ---------- WCAG 对比度实证 ----------
HX = {n: hexes(n) for n in RAMPS}
def sem_hex(key, mode="dark"):
    v = (DARK if mode == "dark" else LIGHT).get(key) or business(mode).get(key)
    if not v:
        return None
    inner = v.replace("var(--of-", "").rstrip(")").strip()
    parts = inner.rsplit("-", 1)
    ramp_name = parts[0]
    slot = parts[1] if len(parts) > 1 else ""
    if ramp_name in HX and slot in HX[ramp_name]:
        return HX[ramp_name][slot]
    if ramp_name == "white":
        return "#ffffff"
    if ramp_name == "black":
        return "#000000"
    return None

pairs = [("foreground", "background", "正文/背景", 4.5),
         ("muted-foreground", "background", "次要文字/背景", 4.5),
         ("primary-foreground", "primary", "主按钮文字/底", 4.5),
         ("card-foreground", "card", "卡片文字/底", 4.5),
         ("secondary-foreground", "secondary", "次按钮文字/底", 4.5),
         ("destructive-foreground", "destructive", "危险按钮文字/底", 4.5),
         ("success-foreground", "success", "成功态文字/底", 4.5),
         ("warning-foreground", "warning", "警示态文字/底", 4.5),
         ("accent-foreground", "accent", "强调态文字/底", 4.5),
         ("log-fg", "log-bg", "日志文字/底", 4.5)]

print("=" * 78)
print("WCAG 2.1 对比度实证（AA 正文阈值 4.5:1）")
print("=" * 78)
report = []
for mode in ("dark", "light"):
    print(f"\n--- {mode} ---")
    for fg, bg, label, thr in pairs:
        h1, h2 = sem_hex(fg, mode), sem_hex(bg, mode)
        if not h1 or not h2:
            print(f"  {label:<18} 跳过（未取到色值）")
            continue
        r = contrast(h1, h2)
        okk = "PASS" if r >= thr else "FAIL"
        report.append((mode, label, r, okk))
        print(f"  {label:<18} {h1} on {h2}  {r:6.2f}:1  {okk}")

fails = [x for x in report if x[3] == "FAIL"]
print(f"\n合计 {len(report)} 组，失败 {len(fails)} 组")
if fails:
    for m, l, r, _ in fails:
        print(f"  ! {m}/{l} = {r:.2f}")

os.makedirs(os.path.join(OUT, "..", "frontend", "src", "styles"), exist_ok=True)
open(os.path.join(OUT, "tokens.json"), "w").write(json.dumps(tokens_json, ensure_ascii=False, indent=2))
open(os.path.join(OUT, "..", "frontend", "src", "styles", "tokens.css"), "w").write(css)
print(f"\n已生成: design/tokens.json ({os.path.getsize(os.path.join(OUT,'tokens.json'))} B)")
print(f"已生成: frontend/src/styles/tokens.css ({len(css)} B)")
