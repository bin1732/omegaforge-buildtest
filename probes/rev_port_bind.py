#!/usr/bin/env python3
"""端口绑定守卫的回退校验：注入回退后，对应用例必须变红。

为什么需要：
    守卫"通过"有两种可能——它真的在验，或者它恒真。判断依据是把它要
    拦的改动注入回去，看它是否变红。注入后先做语法校验：语法坏掉的
    文件会让测试直接崩，"崩"与"抓到"在 pytest 输出里都表现为失败，
    不校验会把假结果当成证据。

用法：
    python3 probes/rev_port_bind.py
退出码 0 = 全部校验点抓到；1 = 至少一个校验点没抓到（守卫是空转）。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "omegaforge" / "server.py"
ERRORS = ROOT / "omegaforge" / "core" / "errors.py"
SIDECAR = ROOT / "scripts" / "sidecar_main.py"
CASE = "tests/test_server_port_bind.py"

READY = '    print(f"Ω OmegaForge Studio → http://{host}:{port}")\n'
BIND = ("    try:\n"
        "        httpd = ThreadingHTTPServer((host, port), Handler)\n"
        "    except OSError as exc:\n"
        "        raise SystemExit(port_bind_error_text(port, exc)) from exc\n")
BARE_BIND = "    httpd = ThreadingHTTPServer((host, port), Handler)\n"

C = CASE + "::"
# 每个校验点都点名它应当变红的用例——只看失败条数说明不了失败的是不是那一条。
ANCHORS: list[tuple[str, Path, str, str, list]] = [
    # A：不捕获绑定异常 —— 启动路径甩出英文栈，桌面端无任何中文线索
    ("A 撤掉绑定失败的捕获", SERVER, BIND, BARE_BIND,
     [C + "test_bind_failure_exits_with_chinese_text",
      C + "test_no_ready_message_when_bind_failed"]),

    # B1：就绪声明早于绑定 —— 绑定失败也会声明"已就绪"
    ("B1 就绪声明挪到绑定之前", SERVER,
     BIND + READY, READY + BIND,
     [C + "test_no_ready_message_when_bind_failed"]),

    # B2：就绪声明晚于阻塞 —— 服务真正起来了却永远等不到就绪提示
    ("B2 就绪声明挪到阻塞之后", SERVER,
     READY + "    httpd.serve_forever()",
     "    httpd.serve_forever()\n" + READY,
     [C + "test_ready_message_precedes_blocking"]),

    # C：占用与权限不足退回同一句 —— 两种场景都照着错的处置去重试
    ("C 端口占用退回通用句", ERRORS,
     '            return (f"服务端口 {port} 已被其他程序占用，"\n'
     '                    f"请先退出占用该端口的程序后重新启动。")',
     '            return f"无法在端口 {port} 启动服务，请更换端口后重试。"',
     [C + "test_error_text_separates_causes"]),

    # D：桌面端入口自行回显英文原始异常
    ("D 桌面端入口回显原始异常", SIDECAR,
     '        print(f"[sidecar] 启动失败：{exc}", flush=True)',
     '        print(f"[sidecar] 启动失败: {exc}", flush=True)',
     [C + "test_sidecar_uses_shared_text"]),
]


def _run_case() -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数会把"收集阶段报错"和"失败的是别的用例"
    都算成抓到。
    """
    return pytest_run(CASE)


def _write(path: Path, text: str) -> bool:
    """原子写入 + 写后校验。

    直接原地改写同一文件时，短时间内反复读写会出现内容不落盘的
    情况：写入调用返回成功，随后读回仍是旧内容。那样"注入生效"的
    确认会失真，校验点结论也就不可信。改走临时文件 + 原子替换，
    并把"读回等于写入内容"当作写入成功的判据。
    """
    for _ in range(6):
        tmp = Path(str(path) + ".revtmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        if path.read_text(encoding="utf-8") == text:
            return True
        time.sleep(0.15)
    return False


def _inject(path: Path, old: str, new: str) -> str | None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        return None
    patched = src.replace(old, new, 1)
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print(f"  [注入后语法损坏，放弃] {exc}")
        return None
    if not _write(path, patched):
        print("  [写入未落盘，放弃]")
        return None
    return src


def main() -> int:
    ok = True
    for name, path, old, new, expect in ANCHORS:
        print(f"--- {name} ---")
        orig = _inject(path, old, new)
        if orig is None:
            print("  注入未命中或语法损坏，锚点无效\n")
            ok = False
            continue
        try:
            assert new in path.read_text(encoding="utf-8"), "注入未生效"
            code, failed, out = _run_case()
            caught, why = verdict(code, failed, expect)
            print(f"  退出码 {code} → {'抓到' if caught else '未抓到'}"
                  f"（{why}）")
            if not caught:
                ok = False
        finally:
            _write(path, orig)
        print()

    code, _failed, out = _run_case()
    print(f"--- 还原后 --- 退出码 {code}")
    if code != 0:
        print(out[-1500:])
        ok = False
    print("\n结论：", "全部锚点抓到" if ok else "存在未抓到的锚点")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
