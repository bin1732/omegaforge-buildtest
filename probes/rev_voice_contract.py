#!/usr/bin/env python3
"""语音模块契约用例的判定校验。

## 七个校验点

 * A 撤掉采样率区间判定 → 6 条
 * B 撤掉 tts 的体积阈值 → 1 条
 * C 撤掉 tts 对 dict 目录的判定 → 1 条
 * D 目录也套体积阈值（防修过头）→ 1 条
 * E 采样率区间收紧到 16k（防修过头）→ 2 条
 * F 两侧体积阈值各写一份（不同源）→ 2 条
 * G 基线

## B 与 C 是两条独立路径

两处都落在 `_is_real`，但分支不同：B 是单个模型/词表文件的体积阈值，C
是目录"至少含一个文件"的判定。撤掉其中一条，另一条照常工作，用例只会红
在对应那一组。合并成一个校验点会把两条失效混成"有红"，看不出是哪条没
了。

## D / E 为什么是反方向的

只验"撤掉修复会变红"证明不了修复刚好够用。D 让词典目录也按体积阈值判
定（目录项是若干小文件，逐个套阈值会误杀正常安装），E 把采样率上界收到
16k（常用采样率被当成异常）。两种写法下，拒收侧的用例一条都不会红，只
有"正常输入必须照常通过"那组会红。

## F 为什么单独占一个校验点

体积下限在 model_size.py 里只定义一次，asr 与 tts 都从那里取。两份字面
量也能让两侧常量相等、用例全绿 —— 那时"同源"只是一个说法，改一侧不会
牵动另一侧。故 F 直接把 asr 侧改成自己写一个数，要求常量比对与 asr 侧
的行为比对同时变红。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。这些文件都在版本控制内，备份缺失时 HEAD 就是可
信的干净版本。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_voice_contract.py"
C = TESTS + "::"
ASR = ROOT / "omegaforge" / "voice" / "asr.py"
TTS = ROOT / "omegaforge" / "voice" / "tts.py"

SR_ON = "        if not (SAMPLE_RATE_MIN <= sr <= SAMPLE_RATE_MAX):"
SR_OFF = "        if False:"
SR_TIGHT = "        if not (SAMPLE_RATE_MIN <= sr <= 16000):"

SZ_ON = "        return p.is_file() and p.stat().st_size >= MODEL_MIN_BYTES"
SZ_OFF = "        return p.is_file()"

DIR_ON = '            return any(f.is_file() for f in p.rglob("*"))'
DIR_OFF = "            return p.exists()"
DIR_SZ = "            return p.stat().st_size >= MODEL_MIN_BYTES"

SHARED = "from omegaforge.voice.model_size import MODEL_MIN_BYTES"
LOCAL = "MODEL_MIN_BYTES = 1_000"

REJ = C + "test_asr_rejects_abnormal_sample_rate"
ACC = C + "test_asr_accepts_normal_sample_rate"
AMP = C + "test_asr_resample_amplification_is_bounded"
TRUNC = C + "test_tts_rejects_empty_and_truncated_models"
DICT_T = C + "test_tts_rejects_empty_dict_dir"
REAL_T = C + "test_tts_accepts_real_install"
SHARE_T = C + "test_asr_and_tts_share_one_size_threshold"
FOLLOW_T = C + "test_asr_size_threshold_follows_shared_definition"

ANCHORS = [
    ("A 撤掉采样率区间判定", [(ASR, SR_ON, SR_OFF)], [REJ]),
    ("B 撤掉 tts 的体积阈值", [(TTS, SZ_ON, SZ_OFF)], [TRUNC]),
    ("C 撤掉 dict 目录判定", [(TTS, DIR_ON, DIR_OFF)], [DICT_T]),
    ("D 目录也套体积阈值（防修过头）", [(TTS, DIR_ON, DIR_SZ)], [REAL_T]),
    ("E 采样率区间收紧到 16k（防修过头）", [(ASR, SR_ON, SR_TIGHT)],
     [ACC, AMP]),
    ("F 两侧体积阈值各写一份", [(ASR, SHARED, LOCAL)], [SHARE_T, FOLLOW_T]),
    ("G 基线", [], []),
]

FILES = [ASR, TTS]


def _restore(path: Path, bak: Path):
    shutil.move(str(bak), str(path))


def self_heal() -> bool:
    """清掉遗留的注入；遇未提交改动即中止。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。

    但"注入残留"与"正在编写的改动"在 `git diff` 下完全同形：两者都是
    工作区与 HEAD 不一致。一律用 HEAD 覆盖，会把当次正在写的源码改动
    一并抹掉，而且抹掉之后症状表现为用例变红，排查方向指向产品代码。
    故这里只还原 .bak（本脚本自己留下的），没有 .bak 又有差异时中止并
    提示，绝不静默回滚。
    """
    for path in FILES:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            _restore(path, bak)
            print(f"  [自愈] 还原 {path.name}")
    dirty = subprocess.run(["git", "diff", "--name-only", "--"] +
                           [str(p.relative_to(ROOT)) for p in FILES],
                           cwd=str(ROOT), capture_output=True, text=True)
    names = (dirty.stdout or "").split()
    if names:
        print("  [中止] 这些文件有未提交改动，无法区分是注入残留还是当次"
              "编写中的改动：")
        for name in names:
            print(f"      {name}")
        print("      先提交或先手工确认，再跑本脚本")
        return False
    return True


def main() -> int:
    if not self_heal():
        return 1
    want = sys.argv[1:]
    bad = []
    for name, edits, expect in ANCHORS:
        if want and name[0] not in want:
            continue
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        missing = False
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：注入点命中 {src.count(old)} 次，"
                      f"须重定位（{path.name}）")
                bad.append(name)
                missing = True
                break
            if path not in paths:
                paths.append(path)
        if missing:
            continue

        baks = []
        try:
            for path in paths:
                bak = Path(str(path) + ".bak")
                shutil.copy2(path, bak)
                baks.append((path, bak))
            for path, old, new in edits:
                src = path.read_text(encoding="utf-8")
                path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed, tail = pytest_run(TESTS)
        finally:
            for path, bak in baks:
                if bak.exists():
                    _restore(path, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:56]}）")
        if not ok:
            bad.append(name)
            print(f"      失败项 {failed[:6]}")
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    ran = len([a for a in ANCHORS if not want or a[0][0] in want])
    print(f"本段校验点 {ran} 个，抓到 {ran - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
