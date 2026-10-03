"""sRGB <-> OKLab/OKLCH 精确转换（Björn Ottosson 标准算法）。

用途：设计 token 单一事实源。所有颜色以 OKLCH 存储，
因为 OKLCH 感知均匀 —— 固定 L 步长生成色阶时视觉间隔一致，
且 oklch(L C H / alpha) 支持原生透明度组合。
"""
import math

M1 = [[0.4122214708, 0.5363325363, 0.0514459929],
      [0.2119034982, 0.6806995451, 0.1073969566],
      [0.0883024619, 0.2817188376, 0.6299787005]]
M1_INV = [[ 4.0767416621, -3.3077115913,  0.2309699292],
          [-1.2684380046,  2.6097574011, -0.3413193965],
          [-0.0041960863, -0.7034186147,  1.7076147010]]
M2 = [[0.2104542553,  0.7936177850, -0.0040720468],
      [1.9779984951, -2.4285922050,  0.4505937099],
      [0.0259040371,  0.7827717662, -0.8086757660]]
M2_INV = [[1.0,  0.3963377774,  0.2158037573],
          [1.0, -0.1055613458, -0.0638541728],
          [1.0, -0.0894841775, -1.2914855480]]


def _mul(m, v):
    return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]


def hex_to_rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def rgb_to_hex(r, g, b):
    def f(c):
        return max(0, min(255, round(c * 255)))
    return '#%02x%02x%02x' % (f(r), f(g), f(b))


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c):
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1 / 2.4)) - 0.055


def hex_to_oklch(h):
    """#rrggbb -> (L, C, H)  L,C in 0..1, H in degrees 0..360"""
    rgb = hex_to_rgb(h)
    lin = [srgb_to_linear(c) for c in rgb]
    lms = _mul(M1, lin)
    lms_ = [math.copysign(abs(v) ** (1 / 3), v) for v in lms]
    lab = _mul(M2, lms_)
    L, a, b = lab
    C = math.hypot(a, b)
    H = math.degrees(math.atan2(b, a)) % 360.0
    return L, C, H


def oklch_to_hex(L, C, H, clip=True):
    """(L, C, H) -> #rrggbb；返回 None 表示超出 sRGB 色域"""
    a = C * math.cos(math.radians(H))
    b = C * math.sin(math.radians(H))
    lms_ = _mul(M2_INV, [L, a, b])
    lms = [v ** 3 for v in lms_]
    lin = _mul(M1_INV, lms)
    rgb = [linear_to_srgb(c) for c in lin]
    if clip:
        if any(c < -1e-4 or c > 1 + 1e-4 for c in rgb):
            return None  # out of sRGB gamut
    return rgb_to_hex(*[max(0.0, min(1.0, c)) for c in rgb])


def in_gamut(L, C, H):
    a = C * math.cos(math.radians(H))
    b = C * math.sin(math.radians(H))
    lms_ = _mul(M2_INV, [L, a, b])
    lms = [v ** 3 for v in lms_]
    lin = _mul(M1_INV, lms)
    return all(-1e-4 <= c <= 1 + 1e-4 for c in lin)


def fmt(L, C, H, p=4):
    """格式化为 CSS oklch() 通道值（不含 oklch() 包裹），供 var 使用"""
    return f"{L:.{p}f} {C:.{p}f} {H:.2f}"


if __name__ == "__main__":
    # 用已知值验证算法正确性
    checks = [
        ("#ffffff", 1.0, 0.0, None),
        ("#000000", 0.0, 0.0, None),
    ]
    print("=== 算法自检（已知真值）===")
    for h, el, ec, eh in checks:
        L, C, H = hex_to_oklch(h)
        ok = abs(L - el) < 1e-3 and abs(C - ec) < 1e-3
        print(f"  {h} -> L={L:.4f} C={C:.4f} H={H:.2f}   {'OK' if ok else 'FAIL'}")
    # 往返精度
    print("\n=== 往返精度（hex -> oklch -> hex）===")
    worst = 0.0
    for h in ["#7c5cff", "#00e5a0", "#ffb020", "#ff5470", "#0b0e14",
              "#121722", "#1a2233", "#070a12", "#8b6cff", "#5a3df0"]:
        L, C, H = hex_to_oklch(h)
        back = oklch_to_hex(L, C, H)
        rgb1, rgb2 = hex_to_rgb(h), hex_to_rgb(back)
        err = max(abs(a - b) for a, b in zip(rgb1, rgb2))
        worst = max(worst, err)
        print(f"  {h} -> oklch({L:.4f} {C:.4f} {H:.2f}) -> {back}  err={err:.6f}")
    print(f"\n最大往返误差: {worst:.8f}  {'PASS' if worst < 1e-6 else 'FAIL'}")
