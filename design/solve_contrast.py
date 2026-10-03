# -*- coding: utf-8 -*-
"""语义色档位求解器：在色阶内搜索满足 WCAG AA 的档位。
不手动拍脑袋，由算法给出达标且最接近设计意图的解。"""
from palette import RAMPS, SCALE, hexes, contrast
from oklch import hex_to_oklch

HX = {n: hexes(n) for n in RAMPS}
ANCHOR = "#7c5cff"

def best_slot(ramp, prefer, fg_hex, thr=4.5, mode="dark"):
    """在 ramp 色阶里找对比度>=thr 且档位最接近 prefer 的解。
    深色模式倾向更亮的档(数字小)，浅色模式倾向更深的档(数字大)。"""
    cands = []
    for slot in SCALE:
        h = HX[ramp][slot]
        if h is None:
            continue
        r = contrast(fg_hex, h)
        if r >= thr:
            cands.append((abs(SCALE.index(slot) - SCALE.index(prefer)), slot, h, r))
    if not cands:
        return None
    cands.sort()
    return cands[0][1], cands[0][2], cands[0][3]

print("=" * 80)
print("语义色档位求解（目标：白/黑字 on 底 >= 4.5:1）")
print("=" * 80)

targets = [
    ("primary",     "brand",   "500", ["#ffffff"]),
    ("destructive", "danger",  "600", ["#ffffff"]),
    ("success",     "success", "600", ["#ffffff", "#000000"]),
    ("warning",     "warning", "600", ["#ffffff", "#000000"]),
]

solutions = {}
for sem, ramp, prefer, fgs in targets:
    print(f"\n--- {sem} (色阶 {ramp}, 设计意图档位 {prefer}) ---")
    # 先展示原意图档位的实际表现
    h0 = HX[ramp][prefer]
    for fg in fgs:
        print(f"  意图档 {prefer}: {h0}  {fg}字 = {contrast(fg, h0):5.2f}:1")
    chosen = None
    for fg in fgs:
        r = best_slot(ramp, prefer, fg)
        if r:
            slot, h, cr = r
            print(f"  求解 -> 档位 {slot} = {h}，{fg}字 = {cr:.2f}:1  PASS")
            if chosen is None or cr < chosen[2] + 0.0:
                chosen = (slot, h, cr, fg)
    if chosen is None:
        print(f"  !! 色阶内无解，需要调整色阶本身")
        solutions[sem] = None
    else:
        solutions[sem] = chosen

print("\n" + "=" * 80)
print("品牌锚点特殊处理：primary 必须保持 #7c5cff 识别度")
print("=" * 80)
h_b = ANCHOR
for fg, nm in [("#ffffff", "白"), ("#000000", "黑")]:
    print(f"  {nm}字 on {h_b} = {contrast(fg, h_b):.2f}:1")
# 找最接近 #7c5cff 且白字达标的 L
print("\n  沿 brand 色阶向下搜索白字达标的档位：")
for slot in SCALE:
    h = HX["brand"][slot]
    c = contrast("#ffffff", h)
    mark = "PASS" if c >= 4.5 else "    "
    print(f"    brand-{slot:<3} {h}  白字 {c:5.2f}:1  {mark}")
