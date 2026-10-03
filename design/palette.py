"""色阶生成器 v2 —— 修复 v1 的三个缺陷：
1. v1 用 in_gamut() 与 oklch_to_hex() 两套标准，结论互相矛盾
   -> v2 统一以 oklch_to_hex() 能否产出为唯一标准（要真的能转成 hex）
2. v1 舍入 brand C 到 4 位导致真色 #7c5cff 被判越界
   -> v2 保留 6 位精度，并对 C 施加 0.985 安全余量
3. v1 surface 用 max_chroma 比例，暗部 C 高达 0.056（#020821，过蓝）
   -> v2 中性色改用绝对彩度，锚定设计文档检验值 0.0136
"""
from oklch import hex_to_oklch, oklch_to_hex, hex_to_rgb

# ---- 唯一色域判定标准：能否真的产出 hex ----
def ok(L, C, H):
    return oklch_to_hex(L, C, H) is not None

def max_chroma(L, H, cap=0.4):
    """二分求最大可用 C，以 ok() 为准，并留安全余量"""
    if not ok(L, 0.0, H):
        return 0.0
    if ok(L, cap, H):
        return cap
    lo, hi = 0.0, cap
    for _ in range(50):
        mid = (lo + hi) / 2
        if ok(L, mid, H):
            lo = mid
        else:
            hi = mid
    return lo * 0.985          # 安全余量，吸收浮点/舍入误差

def chroma_ramp(H, Ls, ratios, cap=0.40):
    """彩色：C 按可用上限的比例取"""
    return [(L, round(max_chroma(L, H, cap) * r, 6), H) for L, r in zip(Ls, ratios)]

def abs_ramp(H, Ls, Cs):
    """中性色：C 用绝对值，并裁到色域内"""
    out = []
    for L, C in zip(Ls, Cs):
        mc = max_chroma(L, H, cap=0.10)
        out.append((L, round(min(C, mc), 6), H))
    return out

# ---- 中性 surface：锚定设计文档检验 H=264.1、暗部 C≈0.0136 ----
SURFACE_H = 264.10
SURFACE_L = [0.1450, 0.1900, 0.2300, 0.2700, 0.3150, 0.3700, 0.4300,
             0.5100, 0.6300, 0.7500, 0.8650, 0.9400, 0.9850]
SURFACE_C = [0.0136, 0.0155, 0.0172, 0.0185, 0.0195, 0.0200, 0.0195,
             0.0175, 0.0145, 0.0115, 0.0085, 0.0055, 0.0030]

BRAND_H = 286.20
BRAND_L = [0.9700, 0.9300, 0.8700, 0.8000, 0.7200, 0.6450, 0.5990,
           0.5450, 0.4800, 0.4100, 0.3400]
BRAND_C_R = [0.14, 0.26, 0.48, 0.72, 0.92, 1.00, 1.00, 0.97, 0.90, 0.78, 0.64]

RAMPS = {
    "surface": abs_ramp(SURFACE_H, SURFACE_L, SURFACE_C),
    "brand":   chroma_ramp(BRAND_H, BRAND_L, BRAND_C_R, cap=0.40),
    "success": chroma_ramp(162.59, [0.97,0.93,0.87,0.81,0.74,0.68,0.62,0.55,0.47,0.39,0.31],
                                  [0.16,0.32,0.60,0.80,0.92,0.97,0.97,0.94,0.86,0.74,0.60], cap=0.33),
    "warning": chroma_ramp(75.04,  [0.97,0.93,0.87,0.81,0.75,0.69,0.63,0.56,0.48,0.40,0.32],
                                  [0.18,0.36,0.66,0.85,0.95,0.98,0.98,0.95,0.86,0.74,0.60], cap=0.30),
    "danger":  chroma_ramp(14.99,  [0.97,0.93,0.87,0.81,0.74,0.68,0.62,0.55,0.47,0.39,0.31],
                                  [0.14,0.28,0.54,0.76,0.90,0.96,0.96,0.93,0.85,0.73,0.59], cap=0.33),
}
SCALE = ["50","100","200","300","400","500","600","700","800","900","950"]
SURFACE_SCALE = ["0","1","2","3","4","5","6","7","8","9","10","11","12"]

def rel_lum(h):
    r, g, b = hex_to_rgb(h)
    f = lambda c: c/12.92 if c <= 0.04045 else ((c+0.055)/1.055)**2.4
    return 0.2126*f(r) + 0.7152*f(g) + 0.0722*f(b)

def contrast(h1, h2):
    a, b = rel_lum(h1), rel_lum(h2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)

def hexes(name):
    steps = SURFACE_SCALE if name == "surface" else SCALE
    return {s: oklch_to_hex(*v) for s, v in zip(steps, RAMPS[name])}

if __name__ == "__main__":
    print("=" * 76)
    print("色阶生成 v2 —— 单一色域标准（能否真实产出 hex）")
    print("=" * 76)
    fail = 0
    for name in RAMPS:
        steps = SURFACE_SCALE if name == "surface" else SCALE
        print(f"\n--- {name} ---")
        prev = None
        for s, (L, C, H) in zip(steps, RAMPS[name]):
            hx = oklch_to_hex(L, C, H)
            if hx is None:
                fail += 1
                print(f"  {name}-{s:<3} FAIL 无法产出 hex  oklch({L} {C} {H})")
                continue
            d = f"  ΔL={L-prev:+.4f}" if prev is not None else ""
            print(f"  {name}-{s:<3} oklch({L:.4f} {C:.4f} {H:6.2f}) = {hx}{d}")
            prev = L
    print(f"\n总失败数: {fail}   {'全部通过' if fail == 0 else '仍有失败'}")

    print("\n" + "=" * 76)
    print("关键校验：品牌色是否还原为 #7c5cff")
    print("=" * 76)
    b = hexes("brand")
    print(f"  brand-600 = {b['600']}   (目标 #7c5cff)")
    print(f"  实测 oklch = {[f'{x:.4f}' for x in hex_to_oklch(b['600'])]}")
    print(f"  目标 oklch = {[f'{x:.4f}' for x in hex_to_oklch('#7c5cff')]}")
    print(f"  色相偏差 = {abs(hex_to_oklch(b['600'])[2] - hex_to_oklch('#7c5cff')[2]):.3f}°")
