# -*- coding: utf-8 -*-
"""生成 OmegaForge Studio 品牌图标（打包必需，不是装饰）。

设计：深色圆角方底 + 发光 Ω 符号。
所有尺寸由 1024 超采样母版降采样得到，保证 32px 下轮廓依然锐利。
"""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "src-tauri", "icons")
os.makedirs(OUT, exist_ok=True)

SS = 1                      # 超采样倍数
# 检验：4096 母版在 GaussianBlur/alpha_composite 后保存会出现中心全黑的合成异常，
# 故固定 1024 母版（已验证 32/128 降采样后 Ω 紫色像素仍稳定保留）。
BASE = 1024 * SS
BRAND = (139, 108, 255)     # Ω 紫 #8b6cff
BRAND_HI = (167, 139, 255)
BG_TOP = (11, 13, 20)       # #0b0d14
BG_BOT = (21, 26, 40)       # #151a28


def find_font():
    cands = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/alibaba-puhuiti/AlibabaPuHuiTi-2-115-Black.ttf",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    import glob
    g = glob.glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True)
    return g[0] if g else None


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_mask(size, radius_ratio=0.22):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1],
                        radius=int(size * radius_ratio), fill=255)
    return m


def build_master():
    S = BASE
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 1) 垂直渐变底
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        c = lerp(BG_TOP, BG_BOT, y / (S - 1))
        for x in range(S):
            px[x, y] = c
    grad.putalpha(rounded_mask(S))
    img.paste(grad, (0, 0), grad)

    # 2) Ω 字形（居中，视觉重心校准）
    fp = find_font()
    if not fp:
        raise SystemExit("未找到可用字体")
    glyph = "Ω"
    size_px = int(S * 0.60)
    font = ImageFont.truetype(fp, size_px)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bbox = d.textbbox((0, 0), glyph, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # Ω 基线偏上，整体下移 2% 做视觉居中
    d.text(((S - w) / 2 - bbox[0], (S - h) / 2 - bbox[1] + S * 0.02),
           glyph, font=font, fill=BRAND + (255,))

    # 3) 外发光：模糊层叠加，营造 forge 的灼热感
    glow = layer.filter(ImageFilter.GaussianBlur(S * 0.03))
    glow = Image.eval(glow.split()[3], lambda v: int(v * 0.55))
    glow_rgba = Image.new("RGBA", (S, S), BRAND + (0,))
    glow_rgba.putalpha(glow)
    img.alpha_composite(glow_rgba)

    # 4) 本体 + 顶部高光（模拟金属锻打的受光面）
    img.alpha_composite(layer)
    hi = layer.copy()
    a = hi.split()[3]
    hi = Image.new("RGBA", (S, S), BRAND_HI + (0,))
    hi.putalpha(Image.eval(a, lambda v: int(v * 0.35)))
    hi = hi.crop((0, 0, S, int(S * 0.52)))
    img.alpha_composite(hi, (0, 0))

    # 5) 内描边，提升小尺寸边界清晰度
    # 注意：绝不可对 edge 调用 putalpha(rounded_mask(S))。
    # 检验该操作会把圆角区域内 alpha 全部置 255，而未绘制处 RGB 为 (0,0,0)，
    # 合成后会把整块中心涂黑、Ω 被完全抹掉，只剩一圈白边。
    # 正确做法：用 edge 自身的 alpha 参与 composite，透明处自然不覆盖下层。
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.22),
        outline=(255, 255, 255, 34), width=max(1, int(S * 0.006)))
    img.alpha_composite(edge)
    return img


def main():
    master = build_master()
    targets = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
    }
    for name, sz in targets.items():
        im = master.resize((sz, sz), Image.LANCZOS)
        im.save(os.path.join(OUT, name))
        print(f"  {name:16s} {sz}x{sz}")

    # ICO：Windows 安装包需要多尺寸
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(os.path.join(OUT, "icon.ico"),
                format="ICO", sizes=ico_sizes)
    print(f"  icon.ico         {len(ico_sizes)} 档")

    # ICNS：macOS 需要
    try:
        master.save(os.path.join(OUT, "icon.icns"), format="ICNS")
        print("  icon.icns        ok")
    except Exception as e:
        print(f"  icon.icns        SKIP: {e}")


if __name__ == "__main__":
    main()
