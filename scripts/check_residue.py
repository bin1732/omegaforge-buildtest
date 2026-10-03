#!/usr/bin/env python3
"""表层残留守卫：任何会被用户看见的开发痕迹都必须在 CI 被拦下。

## 为什么需要

"表层"指的是用户眼睛能看到的全部内容。开发期的调试输出、TODO、
占位文案一旦漏进构建产物，用户看到的就是"没做完的东西"——
这比功能缺失更伤信任。

## 设计取舍（检验教训）

第一版把 `placeholder` 直接当占位文案，结果 11 处误报：
Tailwind 的 `placeholder:` 变体（placeholder:text-muted-foreground）
和输入框的中文提示（placeholder="留空则不修改"）都被误报。
误报的守卫比没有守卫更糟——它会让人习惯性忽略失败。

所以现在分两类：
- FAIL：确定是开发痕迹
- ALLOW：有正当理由且已书面说明，打印出来接受审视但不阻断

退出码 0 = 干净；1 = 有真实残留。
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter

# Windows runner 检验（run104 真实失败）：cp1252 下 print 中文会抛
# UnicodeEncodeError。这里强制 UTF-8，保证脚本单独运行也成立。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT_DEFAULT = os.path.join("frontend", "src")

# 确定是开发痕迹 —— 命中即失败
FAIL_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("console 调试输出", re.compile(r"\bconsole\.(log|debug|info|warn)\b")),
    ("代码中的 TODO/FIXME", re.compile(r"\b(TODO|FIXME|HACK)\b")),
    ("占位假文", re.compile(r"(Lorem|lorem ipsum|example\.com|\bdummy\b)")),
    ("浏览器原生 alert", re.compile(r"(?<![\w.])alert\s*\(")),
    ("危险 HTML 注入", re.compile(r"dangerouslySetInnerHTML")),
    # 检验修正：只拦"整句全英文"的占位符。含中文却夹带技术术语的
    # （如"直接粘贴源 Agent 的 system prompt…"）是正确表述，不是残留。
    (
        "英文占位提示",
        re.compile(r'placeholder="(?![^"\n]*[一-鿿])[^"\n]*[A-Za-z]{4,}[^"\n]*"'),
    ),
    ("写死的测试密钥", re.compile(r"(sk-[A-Za-z0-9]{8,}|api[_-]?key\s*=\s*['\"][^'\"]{8,})")),
]

# 有正当理由、但必须被看见 —— 打印不阻断
# 理由：后端是 Tauri sidecar，只监听本机回环，这是安全边界而非临时调试地址。
ALLOW_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "本机回环地址",
        re.compile(r"(localhost|127\.0\.0\.1)"),
        "sidecar 只监听 127.0.0.1，属设计内安全边界",
    ),
]

SKIP_SUFFIX = (".css", ".json")


def scan(root: str) -> tuple[list[tuple[str, str, int, str]], list[tuple[str, str, int, str, str]]]:
    fails: list[tuple[str, str, int, str]] = []
    allows: list[tuple[str, str, int, str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", "dist", "__pycache__")]
        for fn in filenames:
            if fn.endswith(SKIP_SUFFIX):
                continue
            path = os.path.join(dirpath, fn)
            try:
                lines = open(path, encoding="utf-8").read().splitlines()
            except (OSError, UnicodeDecodeError):
                continue

            for lineno, line in enumerate(lines, 1):
                text = line.strip()
                is_comment = text.startswith(("//", "*", "/*", "#", "<!--"))

                for name, rx in FAIL_RULES:
                    if not rx.search(line):
                        continue
                    # 注释里的 TODO 是给开发者的，不会到用户面前
                    if name == "代码中的 TODO/FIXME" and is_comment:
                        continue
                    fails.append((name, path, lineno, text[:120]))

                for name, rx, reason in ALLOW_RULES:
                    if rx.search(line):
                        allows.append((name, path, lineno, text[:120], reason))

    return fails, allows


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else ROOT_DEFAULT
    if not os.path.isdir(root):
        print(f"扫描目录不存在：{root}")
        return 1

    fails, allows = scan(root)

    if allows:
        print(f"已确认豁免 {len(allows)} 处（有书面理由，不阻断）：")
        for name, path, lineno, text, reason in allows[:10]:
            print(f"  [{name}] {os.path.relpath(path, root)}:{lineno}  ← {reason}")
            print(f"      {text}")
        print()

    if not fails:
        print(f"表层残留扫描通过 ✓（{root}）")
        return 0

    counter = Counter(n for n, _, _, _ in fails)
    print(f"发现 {len(fails)} 处真实残留：")
    for name, cnt in counter.most_common():
        print(f"  {name}: {cnt}")
    print()
    for name, path, lineno, text in fails[:40]:
        print(f"[{name}] {os.path.relpath(path, root)}:{lineno}")
        print(f"    {text}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
