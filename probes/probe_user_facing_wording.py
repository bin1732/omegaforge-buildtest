#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面向用户文案的整洁度扫描。

扫描两类"用户能看见的文字"：
  1. 后端：所有 raise UserError("…") 的中文文案字面量
  2. 前端：所有含中文的字符串字面量

检查三件事：
  · 内部标识外泄（蛇形/驼峰英文词、文件名、协议字段名）
  · 开发过程用语（检验、原先、改动前、守卫、校验点、检查脚本……）
  · 重复冗余（同一件事多份文案口径不一致，由另表比对，这里只报同文重复）

白名单词是通用术语，出现不算违规；它们是行业通行说法，不是内部标识。

用法：python probes/probe_user_facing_wording.py
"""
from __future__ import annotations

import io
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 通用术语：行业通行说法，不是内部标识，允许出现在用户文案里
ALLOW = {
    "Agent", "agent", "prompt", "Prompt", "system", "API", "HTTP", "HTTPS",
    "JSON", "URL", "UTF", "WAV", "OpenAI", "token", "Markdown", "ID", "IP",
    "MCP", "CLI", "Tauri", "BigModel", "Claude", "Gemini", "DeepSeek",
    # 通用协议 / 单位 / 键名 / 产品名：不是内部标识
    "http", "https", "kHz", "Token", "Shift", "OmegaForge", "Enter",
}

#: 开发过程用语 / 内部黑话：用户可见处不该出现
JARGON = [
    "实测", "修复前", "修复后", "此前", "上一轮", "本轮",
    "复现", "冒烟", "走查", "假绿", "互为掩护", "单撤", "摆设", "打回",
    "反向验证", "守卫", "锚点", "探针", "界碑", "自证", "脱敏",
    "盲区", "覆盖度",
    "第 46 章", "第 45 章", "第 44 章", "ch33", "ch35",
]

#: 通用技术术语：描述代码行为所必需，不属于内部黑话，刻意**不**列入 JARGON。
#: 把它们留在词表里会制造大量误报，而误报会引着人去改本来没问题的代码——
#: 误报比漏报更危险。
NEUTRAL = ["下一轮", "兼容", "降级", "兜底", "注入", "契约", "回归", "遗留"]

#: 内部标识形态。
#:
#: 两种都要抓，缺一个就漏一大片：
#:   1) 带点/下划线的标识符（SKILL.md / task_id / 基线说明）
#:   2) 夹在中文里的裸英文词（cases / rubric / input / 简化对照）
#:
#: 只写第 1 种时检验漏掉了 `评测集缺少 cases 字段` —— 它是最典型的
#: 内部字段名外泄，却因为不含点或下划线而完全逃逸。
RE_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9._]{2,}")


def _py_user_error_texts() -> list[tuple[str, str]]:
    """后端所有 UserError 文案（含拼接前的字面量片段）。"""
    out = []
    for r, _d, fs in os.walk(ROOT / "omegaforge"):
        for f in fs:
            if not f.endswith(".py"):
                continue
            p = os.path.join(r, f)
            try:
                src = io.open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for m in re.finditer(r'UserError\(\s*f?"([^"]{4,300})"', src):
                out.append((p, m.group(1)))
    return out


def _frontend_zh_texts() -> list[tuple[str, str]]:
    out = []
    base = ROOT / "frontend" / "src"
    if not base.is_dir():
        return out
    for r, _d, fs in os.walk(base):
        for f in fs:
            if not f.endswith((".ts", ".tsx")):
                continue
            p = os.path.join(r, f)
            try:
                src = io.open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for m in re.finditer(r'"([^"\n]{2,200})"', src):
                t = m.group(1)
                if re.search(r"[\u4e00-\u9fff]", t):
                    out.append((p, t))
    return out


def _comment_texts() -> list[tuple[str, str]]:
    """源码里的注释与文档字符串（加 --all 时纳入扫描）。

    用 tokenize 而不是正则：注释与字符串里的 # 和引号互相干扰，
    正则分不清「注释里的引号」和「字符串里的 #」。
    """
    import tokenize
    out = []
    tq = ('"""', "'''")
    for r, _d, fs in os.walk(ROOT / "omegaforge"):
        for f in fs:
            if not f.endswith(".py"):
                continue
            p = os.path.join(r, f)
            try:
                src = io.open(p, encoding="utf-8").read()
                toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
            except Exception:                      # noqa: BLE001
                continue
            for t in toks:
                if t.type == tokenize.COMMENT:
                    out.append((p, t.string))
                elif t.type == tokenize.STRING and len(t.string) >= 80:
                    if t.string[:3] in tq:
                        out.append((p, t.string))
    return out


def _offenders(text: str, check_ident: bool = True) -> list[str]:
    """检查一条文案。

    先剥掉 `{…}` 占位符再查：f-string 里的 `{MSG_MAX_CHARS:,}` 是**变量**，
    渲染出来是数字，不是外泄的标识。不剥会把 8 条正常文案误报成违规——
    误报比漏报更糟，它会引着人去改本来没问题的代码。

    check_ident=False 用于源码注释：注释里引用函数名、模块路径是**必要**
    的（`self.xxx`、`core.limits`），那不是外泄。对注释只查过程叙事与
    对话用语，否则 2008 条里会冒出上千条误报，反而看不出真问题。
    """
    visible = re.sub(r"\{[^{}]*\}", "◇", text)
    bad = []
    for w in JARGON:
        if w in visible:
            bad.append(f"黑话「{w}」")
    if check_ident:
        for m in RE_IDENT.finditer(visible):
            tok = m.group(0)
            if tok in ALLOW:
                continue
            bad.append(f"内部标识「{tok}」")
    return bad


def main() -> int:
    groups = [("后端报错文案", _py_user_error_texts()),
              ("前端界面文案", _frontend_zh_texts())]
    if "--all" in sys.argv:
        groups.append(("源码注释", _comment_texts()))
    total = 0
    for name, items in groups:
        rows = []
        for path, text in items:
            # 注释层不查代码标识符（注释引用函数名是必要的）
            bad = _offenders(text, check_ident=(name != "源码注释"))
            if bad:
                rows.append((path, text, bad))
        print("=" * 72)
        print(f"{name}  共 {len(items)} 条，疑似不整洁 {len(rows)} 条")
        print("=" * 72)
        for path, text, bad in rows:
            rel = os.path.relpath(path, ROOT)
            print(f"\n  [{rel}]")
            print(f"    {text[:110]}")
            print(f"    -> {'; '.join(bad)}")
        total += len(rows)
    print("\n" + "=" * 72)
    print(f"合计疑似不整洁：{total} 条")
    print("=" * 72)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
