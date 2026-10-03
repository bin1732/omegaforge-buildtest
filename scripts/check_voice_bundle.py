# -*- coding: utf-8 -*-
"""语音内置守卫：需求契约、下载供给、打包产物三处必须对得上。

## 为什么要有这个守卫

语音"装完了却用不了"不是一类错误，而是三处定义各自漂移的合成结果：

  运行时需求（tts.status）  要 model.onnx / tokens.txt / lexicon.txt / dict
  下载清单（spec.SPEC）      曾经只给 model.onnx / tokens.txt
  打包产物（_internal/models）可能压根没带

三处任一处单独看都成立：下载器对自己清单里的文件负责、status 如实报缺什么、
打包步骤只管复制目录。用户拿到的是"点一键安装→显示完成→点朗读→报模型未
下载完整"，而排查时三处都没有错。

本守卫把这三者钉在一起：

  1. 需求必须被下载清单覆盖（uncovered_requirements 为空）
  2. 打包产物里模型必须真的齐备（不是空目录、不是占位文件）
  3. 读不到源码/产物时必须判失败——否则路径写错时本守卫会退化成恒真

## 判定口径

- 需求覆盖：永远校验，与是否内置无关。
- 产物齐备：_internal 存在时校验；不存在时按"未打包"处理并打印，
  不静默通过——静默通过会让"忘了内置"与"内置成功"在报告上无法区分。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.voice import spec as voice_spec  # noqa: E402

MANIFEST = "voice_manifest.json"


def check_coverage() -> list:
    """需求是否被下载清单覆盖。返回问题列表。"""
    out = []
    for kind in voice_spec.SPEC:
        uncovered = voice_spec.uncovered_requirements(kind)
        if uncovered:
            out.append(
                f"{kind}：下载清单未覆盖运行时需求 {'、'.join(uncovered)}"
                f"（清单={voice_spec.SPEC[kind]['files']}）")
    return out


def check_bundle(internal: str) -> tuple[list, list]:
    """校验打包产物里的语音资源。返回 (问题, 说明)。"""
    problems: list = []
    notes: list = []
    p = Path(internal)
    if not p.is_dir():
        # 读不到必须判失败：路径写错时若静默通过，本守卫等于不存在。
        problems.append(f"未找到 _internal：{internal}")
        return problems, notes

    models = p / "models"
    if not models.is_dir():
        problems.append(f"_internal 下没有 models/：{models}（语音未内置）")
        return problems, notes

    manifest = models / MANIFEST
    if manifest.is_file():
        import json
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            problems.append(f"语音清单读不出来：{e}")
            data = {}
        for s in data.get("skipped", []):
            notes.append(f"内置时跳过 {s.get('kind')}：{s.get('reason')}")
        for m in data.get("models", []):
            notes.append(f"内置 {m.get('kind')}：{m.get('bytes')} 字节")
    else:
        problems.append(f"缺少语音清单 {MANIFEST}——无法判断内置了什么")

    # 逐个模型按运行时需求复核，而不是只数目录里有多少文件。
    for kind in voice_spec.SPEC:
        name = voice_spec.SPEC[kind]["name"]
        mdir = models / name
        if not mdir.is_dir():
            problems.append(f"{kind} 模型目录缺失：{mdir}")
            continue
        for entry in voice_spec.required_entries(kind):
            e = mdir / entry
            if voice_spec.is_dir_entry(kind, entry):
                ok = e.is_dir() and any(q.is_file() for q in e.rglob("*"))
            else:
                # 下限按条目取：词表/词典是真的小文件（tokens.txt 仅 655
                # 字节），套用模型文件的阈值会让"内置成功"被判成占位文件。
                ok = (e.is_file()
                      and e.stat().st_size >= voice_spec.min_bytes_for(entry))
            if not ok:
                problems.append(f"{kind} 缺 {entry}（或为占位文件）：{e}")
    return problems, notes


def main() -> int:
    internal = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "src-tauri" / "_internal")

    problems = check_coverage()
    if not problems:
        print("OK 需求清单已被下载清单覆盖")
    else:
        for x in problems:
            print(f"FAIL {x}")

    bp, notes = check_bundle(internal)
    for n in notes:
        print(f"  · {n}")
    for x in bp:
        print(f"FAIL {x}")

    allp = problems + bp
    if allp:
        print(f"\n语音内置守卫：{len(allp)} 项不通过")
        return 1
    print("\n语音内置守卫通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
