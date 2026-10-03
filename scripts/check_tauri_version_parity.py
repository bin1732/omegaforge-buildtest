#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 tauri 的 npm 包与 Rust crate 的次版本号一致。

## 管的是什么

tauri CLI 在打包前会比对两侧版本，次版本号不同就直接中止并报
``Found version mismatched Tauri packages``。前端与 Rust 本身都没有任何
错误——代码、构建、测试全绿，失败只发生在打包这一步。

## 为什么要单独查

这个中止很难诊断：打包走 tauri-action 时输出只进 Actions UI，而该日志
托管在 Azure Blob，对外下载一律 403。故打包改用显式 CLI 并把输出落盘。

把校验提到打包之前，版本漂移就会在几秒内报出，不必等一整轮编译。

## 判定

读不到任一侧的版本必须判失败：缺失会让比对恒等成立，而"没有版本可比对"
和"版本一致"的结论完全一样。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# （npm 包名, crate 名）：两侧是同一套能力的不同语言绑定，必须同步升级。
PAIRS = (
    ("@tauri-apps/api", "tauri"),
    ("@tauri-apps/plugin-shell", "tauri-plugin-shell"),
)


def npm_version(pkg: Path, name: str) -> str | None:
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    spec = (data.get("dependencies") or {}).get(name) or ""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", spec)
    return m.group(0) if m else None


def crate_version(cargo: Path, name: str) -> str | None:
    if not cargo.exists():
        return None
    text = cargo.read_text(encoding="utf-8")
    # 匹配 `name = { version = "x.y", ... }` 与 `name = "x.y"` 两种写法，
    # 并跳过被注释掉的行——注释里常出现"不要写成 x.y"这类示例版本。
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        # 必须按整名匹配：`tauri` 是 `tauri-build` 的前缀，用 startswith 会
        # 先命中后者并返回它的版本。于是改 tauri 的版本时本检查读到的仍是
        # tauri-build 的——改与不改都报一致，等于把病藏起来。
        m = re.match(r'^%s\s*=' % re.escape(name), line)
        if not m:
            continue
        v = re.search(r'version\s*=\s*"([^"]+)"', line)
        if v:
            return v.group(1)
        v = re.match(r'^%s\s*=\s*"([^"]+)"' % re.escape(name), line)
        if v:
            return v.group(1)
    return None


def minor(v: str | None) -> tuple[int, int] | None:
    if not v:
        return None
    parts = v.split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def main() -> int:
    pkg = ROOT / "frontend" / "package.json"
    cargo = ROOT / "src-tauri" / "Cargo.toml"
    bad = []
    for npm_name, crate_name in PAIRS:
        nv = npm_version(pkg, npm_name)
        cv = crate_version(cargo, crate_name)
        if nv is None or cv is None:
            bad.append(f"{npm_name} / {crate_name}：有一侧读不到版本"
                       f"（npm={nv!r}, crate={cv!r}）—— 读不到不能判通过，"
                       f"它和『版本一致』的结论一样")
            continue
        mn, mc = minor(nv), minor(cv)
        if mn is None or mc is None:
            # 只写到 major（如 crate 写 "2"）时没有次版本可比。浮动 major
            # 会让两侧各自解析到不同 minor，打包即被中止；而不写次版本恰好
            # 会让本检查判"一致"，等于把病藏起来。
            bad.append(f"{npm_name} {nv} 与 {crate_name} {cv}：至少一侧没写"
                       f"次版本号，无法比对——浮动会让两侧漂到不同 minor")
            continue
        if mn != mc:
            bad.append(f"{npm_name} {nv} 与 {crate_name} {cv} 次版本不一致："
                       f"打包会被 tauri CLI 中止，而前端与 Rust 都不会报错")
        else:
            print(f"[一致] {npm_name} {nv} ~ {crate_name} {cv}")

    if bad:
        print("\n[不一致] %d 处" % len(bad))
        for b in bad:
            print("  -", b)
        return 1
    print("tauri 版本一致性检查通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
