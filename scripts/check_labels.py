#!/usr/bin/env python3
"""表层守卫：后端真实字段必须有中文标签。

## 为什么需要

前端 Structured 组件直接渲染后端 JSON 的键名。键名是英文 snake_case
（persona_genes、baseline_comparable…），没有中文映射就会把开发术语
摆到用户面前——这是最典型的表层残留。

## 怎么保证真

键名不是手写清单，而是从后端 dataclass 真实导入后取出来的字段集合。
后端加了字段而前端没补翻译，这里直接失败。

退出码 0 = 全部覆盖；1 = 有未翻译字段。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Windows runner 检验（run104 真实失败）：默认 stdout 编码是 cp1252，
# 下面 print 中文标题会抛 UnicodeEncodeError，导致"守卫自己崩溃"被误报成
# "有字段未翻译"。这里强制 UTF-8 —— 脚本被单独运行也必须成立，
# 不能只依赖 CI 注入 PYTHONIOENCODING。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

TS_LABELS = os.path.join(ROOT, "frontend", "src", "lib", "labels.ts")


def backend_keys() -> dict[str, set[str]]:
    """从后端真实数据结构抽取字段名。"""
    import dataclasses

    out: dict[str, set[str]] = {}

    # 蒸馏产物：Genome / DistillReport
    from omegaforge.distill.genome import Genome
    from omegaforge.distill.engine import DistillReport

    out["Genome"] = {f.name for f in dataclasses.fields(Genome)}
    out["DistillReport"] = {f.name for f in dataclasses.fields(DistillReport)}

    # 对照组（报告里会带这几个键）
    from omegaforge.domain.baseline import Baseline

    out["Baseline"] = set(Baseline("naive", "", "", "").to_dict().keys())

    # 运行事件流：真实落盘的事件键
    from omegaforge.core.run import RunStore

    out["RunStore"] = set(RunStore.EVENT_KEYS) if hasattr(RunStore, "EVENT_KEYS") else set()

    return out


def frontend_labels() -> set[str]:
    """解析 labels.ts 里 FIELD_LABELS 的键。"""
    src = open(TS_LABELS, encoding="utf-8").read()
    m = re.search(r"const FIELD_LABELS:\s*Record<string,\s*string>\s*=\s*\{(.*?)\n\}",
                  src, re.S)
    if not m:
        raise SystemExit("labels.ts: 找不到 FIELD_LABELS 定义")
    body = m.group(1)
    # 去掉注释行，避免注释里的英文被当成键
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.M))


def main() -> int:
    have = frontend_labels()
    groups = backend_keys()

    missing: list[tuple[str, str]] = []
    covered = 0
    for group, keys in groups.items():
        for k in sorted(keys):
            if k in have:
                covered += 1
            else:
                missing.append((group, k))

    print(f"后端真实字段：{covered + len(missing)} 个，已翻译 {covered} 个")
    if missing:
        print("\n未翻译字段（会原样显示英文给用户）：")
        for group, k in missing:
            print(f"  [{group}] {k}")
        print("\n请在 frontend/src/lib/labels.ts 的 FIELD_LABELS 中补齐。")
        return 1
    print("全部覆盖 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
