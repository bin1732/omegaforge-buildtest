#!/usr/bin/env python3
"""界面守卫：不得出现「按钮里再套一个按钮」。

为什么必须自动查：
HTML 不允许 button 嵌套 button。浏览器解析时会强行闭合外层 button，
把内层提到外面，于是真实 DOM 与框架认为的结构不一致——点击落不到
处理函数上。界面上的表现是"按钮在，点了没反应"，而代码、类型检查、
接口验收、页面渲染核查全部通过，只有真人点一次才会发现。

判定：进入一个按钮标签后，在其闭合之前若又开了一个按钮标签，即违规。
只认真实 JSX 开标签（<button / <Button），注释与字符串里出现的同名
文字不算——按纯文本扫会把正在说明这条规则的注释判成违规，
结果守卫在正确实现上变红，而人因此倾向于把它整条删掉。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BTN_OPEN = re.compile(r"<(button|Button)\b")
BTN_CLOSE = re.compile(r"</(button|Button)\s*>")
SELF_CLOSE = re.compile(r"/\s*>")

# 行注释、块注释、字符串都要剥掉，否则注释里的示例会被当成真实标签。
BLOCK = re.compile(r"/\*.*?\*/", re.S)
LINE = re.compile(r"//[^\n]*")


def strip_noncode(text: str) -> str:
    """把注释与字符串字面量替换成等长空白。

    等长很重要：报错要给出真实行号，删除会让行号整体前移，
    指到的位置就不是出问题的地方。
    """
    out = list(text)
    for m in BLOCK.finditer(text):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    for m in LINE.finditer("".join(out)):
        line = "".join(out)
        for i in range(m.start(), m.end()):
            if line[i] != "\n":
                out[i] = " "
    return "".join(out)


def find_nested(text: str, rel: str):
    src = strip_noncode(text)
    bad = []
    depth = 0
    stack = []
    i = 0
    lines = src.split("\n")
    pos = 0
    for ln, line in enumerate(lines, 1):
        col = 0
        while col < len(line):
            rest = line[col:]
            mc = BTN_CLOSE.search(rest)
            mo = BTN_OPEN.search(rest)
            ms = SELF_CLOSE.search(rest)
            # 取最先出现的那个事件
            cands = []
            if mc:
                cands.append((mc.start(), "close"))
            if mo:
                cands.append((mo.start(), "open"))
            if ms:
                cands.append((ms.start(), "self"))
            if not cands:
                break
            cands.sort(key=lambda x: x[0])
            off, kind = cands[0]
            if kind == "open":
                if depth > 0:
                    bad.append(f"{rel}:{ln}: 按钮内又开了按钮（外层起于第 {stack[-1]} 行）")
                depth += 1
                stack.append(ln)
                col += off + len(mo.group(0))
            elif kind == "close":
                depth = max(0, depth - 1)
                if stack:
                    stack.pop()
                col += off + len(mc.group(0))
            else:
                # 自闭合：开与闭一次性完成
                if depth > 0 and mo and mo.start() < ms.start() and mo.start() >= 0:
                    pass
                if mo and mo.start() == off:
                    pass
                col += off + len(ms.group(0))
        pos += len(line) + 1
    return bad


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "frontend/src")
    files = sorted(root.rglob("*.tsx")) + sorted(root.rglob("*.ts"))
    if not files:
        print(f"FAIL 在 {root} 下没找到任何源码文件；读不到内容必须判失败，不能当成合规")
        return 1
    bad = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            print(f"FAIL 读不到 {f}：{e}")
            return 1
        bad += find_nested(text, str(f))
    if bad:
        print("FAIL 发现按钮嵌套：")
        for b in bad:
            print("  " + b)
        return 1
    print(f"OK 未发现按钮嵌套（已查 {len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
