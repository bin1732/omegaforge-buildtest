from oklch import hex_to_oklch, oklch_to_hex, in_gamut

print("=" * 68)
print("A. 设计文档 UI_UX_DESIGN.md 声明的三级深度")
print("=" * 68)
doc = [("bg 背景", "#0b0e14"), ("panel 面板", "#121722"), ("input 输入层", "#1a2233")]
prev = None
for name, h in doc:
    L, C, H = hex_to_oklch(h)
    d = f"  ΔL={L-prev:.4f}" if prev is not None else ""
    print(f"  {name:<14} {h}  L={L:.4f}  C={C:.4f}  H={H:6.2f}{d}")
    prev = L

print()
print("=" * 68)
print("B. 前端 index.html 实际实现的五级（--bg/bg2/panel/panel2/panel3）")
print("=" * 68)
code = [("--bg", "#070a12"), ("--bg2", "#0a0e18"), ("--panel", "#10151f"),
        ("--panel2", "#161d2b"), ("--panel3", "#1c2434")]
prev = None
Ls = []
for name, h in code:
    L, C, H = hex_to_oklch(h)
    Ls.append(L)
    d = f"  ΔL={L-prev:+.4f}" if prev is not None else ""
    print(f"  {name:<10} {h}  L={L:.4f}  C={C:.4f}  H={H:6.2f}{d}")
    prev = L
steps = [Ls[i+1] - Ls[i] for i in range(len(Ls)-1)]
print(f"\n  L 步进序列: {[f'{s:.4f}' for s in steps]}")
print(f"  步进极差  : {max(steps)-min(steps):.4f}  (感知均匀应为 0)")
print(f"  H 漂移范围: 需检查色相是否一致")

print()
print("=" * 68)
print("C. 品牌主色：文档 vs 代码 vs 硬编码 — 三方不一致")
print("=" * 68)
for label, h in [("文档 Ω紫", "#7c5cff"), ("CSS 变量 --acc", "#8b6cff"),
                 ("JS 硬编码", "#7c5cff"), ("渐变 --grad-acc", "#5a3df0")]:
    L, C, H = hex_to_oklch(h)
    print(f"  {label:<16} {h}  L={L:.4f}  C={C:.4f}  H={H:6.2f}")
