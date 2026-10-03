#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验：voice 层两处修复的守卫必须真能抓到回归。

判据是"注入后测试**以 failed 结束**"，不是"rc≠0"；每次注入后强制
ast.parse 校验语法，防止"测试没跑起来"被误读成"抓到"。

校验点
----
A  撤掉 asr 的样本数判定（照单全收）                                -> 必须红
B  判定挪到**物化之后**（先 unpack 再判 = 上限只挡文案不挡内存）    -> 必须红
C  tts 空文本改回 ValueError("text required")                        -> 必须红
D  撤掉 asr 的采样率区间校验（防修过头：不得被上限判定顶替）        -> 必须红
E  判据退回「秒」而不是「样本数」（48kHz 下放行 1440 万样本）       -> 必须红

关于 B 的说明：上一版 B 写作"判定时机挪到 readframes 之后"，**检验未抓到**，
因为 readframes 返回紧凑 bytes、本身不放大（17.9x 的放大点在 struct.unpack）。
那一版是错的校验点——它把"理由站不住"当成了"回归能被抓到"。本轮改成真正
可观测的形态：仍然抛 UserError（功能测试照绿），但峰值炸掉，只有内存守卫
能抓到。这才是"判定必须在物化之前"这条性质的唯一有效证据。
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASR = ROOT / "omegaforge" / "voice" / "asr.py"
TTS = ROOT / "omegaforge" / "voice" / "tts.py"
GUARD = "tests/test_voice_boundary.py"
C = GUARD + "::"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402


_GUARD_BLOCK = """        nframes = w.getnframes()
        if nframes > MAX_PCM_SAMPLES:
            dur = int(nframes / sr) if sr else 0
            limit = int(MAX_PCM_SAMPLES / sr) if sr else 0
            raise UserError(
                f"音频过长（约 {dur} 秒，{sr // 1000}kHz 下单次上限约 "
                f"{limit} 秒），请把更长的录音分段发送")
        return w.readframes(nframes), sr"""


def _injections():
    return [
        ("A 撤掉 asr 样本数判定", ASR, (
            _GUARD_BLOCK,
            """        return w.readframes(w.getnframes()), sr""",
            [C + "test_message_names_seconds_and_limit",
                                 C + "test_old_seconds_cap_case_is_now_rejected",
                                 C + "test_over_budget_rejected",
                                 C + "test_rejection_peak_is_bounded"],
        )),
        ("B 判定挪到物化之后", ASR, (
            _GUARD_BLOCK,
            """        nframes = w.getnframes()
        data = w.readframes(nframes)
        if nframes > MAX_PCM_SAMPLES:
            _ = struct.unpack(f"<{len(data) // 2}h", data)
            raise UserError("音频过长")
        return data, sr""",
            [C + "test_message_names_seconds_and_limit",
                              C + "test_rejection_peak_is_bounded"],
        )),
        ("E 判据退回秒", ASR, (
            """        if nframes > MAX_PCM_SAMPLES:""",
            """        if nframes / sr > 300:""",
            [C + "test_message_names_seconds_and_limit",
                      C + "test_old_seconds_cap_case_is_now_rejected",
                      C + "test_over_budget_rejected",
                      C + "test_rejection_peak_is_bounded"],
        )),
        ("D 撤掉 asr 采样率区间校验", ASR, (
            """        if not (SAMPLE_RATE_MIN <= sr <= SAMPLE_RATE_MAX):
            raise UserError(
                f"音频采样率异常（{sr}Hz），请提供 "
                f"{SAMPLE_RATE_MIN // 1000}kHz～{SAMPLE_RATE_MAX // 1000}kHz 的音频"
            )
""",
            "",
            [C + "test_format_checks_not_superseded"],
        )),
        ("C tts 空文本改回 ValueError", TTS, (
            """        if not text or not text.strip():
            raise UserError("要朗读的文字不能为空")""",
            """        if not text.strip():
            raise ValueError("text required")""",
            [C + "test_empty_text_gives_chinese_user_error",
                                      C + "test_message_is_chinese_not_english",
                                      C + "test_value_error_not_leaked"],
        )),
    ]


def run() -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数说明不了失败的是不是预期那一条。
    """
    return pytest_run(GUARD)


def failed_count(out: str) -> int:
    m = re.findall(r"(\d+) failed", out)
    return int(m[-1]) if m else 0


def main() -> int:
    print("=" * 72)
    print("第 46 章续反向验证：voice 层两处修复")
    print("=" * 72)

    rc, _f, out = run()
    print(f"\n[基线] rc={rc} failed={failed_count(out)}")
    if rc != 0:
        print("基线就不干净，无法反向验证")
        return 1

    origs = {p: p.read_text(encoding="utf-8") for _, p, _ in _injections()}
    results = {}
    try:
        for name, path, (old, new, expect) in _injections():
            src = origs[path]
            if old not in src:
                print(f"\n[{name}] ✗ 锚点失效：注入点未找到（锚点未执行，\n                  不是\"跑了没抓到\"，必须重定位）")
                results[name] = False
                continue
            patched = src.replace(old, new, 1)
            try:
                ast.parse(patched)
            except SyntaxError as e:
                print(f"\n[{name}] 注入后语法错误: {e} —— 记为未抓到")
                results[name] = False
                continue
            path.write_text(patched, encoding="utf-8")
            rc, failed, out = run()
            ok, why = verdict(rc, failed, expect)
            print(f"\n[{name}] rc={rc} {'抓到' if ok else '未抓到'}（{why}）")
            results[name] = ok
            path.write_text(src, encoding="utf-8")   # 立即还原，避免锚点互相污染
    finally:
        for p, s in origs.items():
            p.write_text(s, encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"反向验证: {sum(results.values())}/{len(results)} 锚点抓到")
    print("=" * 72)
    for k, v in results.items():
        print(f"  {'抓到  ' if v else '未抓到'} {k}")

    rc, _f, out = run()
    print(f"\n[还原校验] rc={rc} failed={failed_count(out)}")
    return 0 if all(results.values()) and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
