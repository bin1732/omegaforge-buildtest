# -*- coding: utf-8 -*-
"""语音内置守卫的自检（不依赖 pytest）。

被验证的形态：
  1. 覆盖关系正常时 check_coverage 为空
  2. 下载清单被改回旧两文件形态 -> 必须报未覆盖
  3. _internal 不存在 -> 必须判失败（静默通过会让守卫退化成恒真）
  4. 产物完整 -> 通过，并列出内置条目
  5. 产物缺 lexicon.txt -> 必须点名
  6. 产物缺 dict/ 或 dict/ 为空 -> 必须点名（空目录不算齐备）
  7. 占位文件（过小）-> 必须点名
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "scripts" / "check_voice_bundle.py"


def load():
    s = importlib.util.spec_from_file_location("cvb", str(SCRIPT))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def build_models(root: Path, kind_files: dict, omit=(), tiny=()) -> None:
    root.mkdir(parents=True, exist_ok=True)
    from omegaforge.voice import spec as vs
    for kind, files in kind_files.items():
        name = vs.SPEC[kind]["name"]
        for f in files:
            if f.split("/")[0] in omit:
                continue
            p = root / name / f
            p.parent.mkdir(parents=True, exist_ok=True)
            size = 5 if f.split("/")[0] in tiny else 120_000
            p.write_bytes(b"M" * size)


def main() -> int:
    import tempfile, json
    from omegaforge.voice import spec as vs

    m = load()
    fails = []

    # 1 覆盖正常
    if m.check_coverage():
        fails.append(f"覆盖关系应为空，实际 {m.check_coverage()}")
    else:
        print("  [1] 覆盖关系正常 OK")

    # 2 清单改回旧形态
    saved = list(vs.SPEC["tts"]["files"])
    try:
        vs.SPEC["tts"]["files"] = ["model.onnx", "tokens.txt"]
        c = m.check_coverage()
        if not c or "lexicon.txt" not in c[0]:
            fails.append(f"清单不完整应报未覆盖，实际 {c}")
        else:
            print("  [2] 清单不完整 -> 报未覆盖 OK")
    finally:
        vs.SPEC["tts"]["files"] = saved

    base = Path(tempfile.mkdtemp(prefix="vbundle-"))

    # 3 _internal 不存在
    p, _ = m.check_bundle(str(base / "nope"))
    if not p:
        fails.append("_internal 不存在应判失败（否则守卫恒真）")
    else:
        print("  [3] _internal 缺失 -> 失败 OK")

    # 4 产物完整
    files = {k: list(v["files"]) for k, v in vs.SPEC.items()}
    good = base / "good"
    build_models(good / "models", files)
    (good / "models" / "voice_manifest.json").write_text(
        json.dumps({"models": [{"kind": "asr", "bytes": 1}], "skipped": []}),
        encoding="utf-8")
    p, notes = m.check_bundle(str(good))
    if p:
        fails.append(f"完整产物应通过，实际 {p}")
    else:
        print(f"  [4] 完整产物通过 OK（{len(notes)} 条说明）")

    # 5 缺 lexicon.txt
    bad5 = base / "bad5"
    build_models(bad5 / "models", files, omit=("lexicon.txt",))
    p, _ = m.check_bundle(str(bad5))
    if not any("lexicon.txt" in x for x in p):
        fails.append(f"缺 lexicon.txt 应点名，实际 {p}")
    else:
        print("  [5] 缺 lexicon.txt -> 点名 OK")

    # 6 dict 为空目录
    bad6 = base / "bad6"
    build_models(bad6 / "models", files)
    (bad6 / "models" / "vits-melo-tts-zh-en" / "dict").mkdir(parents=True,
                                                             exist_ok=True)
    for f in (bad6 / "models" / "vits-melo-tts-zh-en" / "dict").rglob("*"):
        f.unlink()
    p, _ = m.check_bundle(str(bad6))
    if not any("dict" in x for x in p):
        fails.append(f"dict 为空应点名，实际 {p}")
    else:
        print("  [6] dict 空目录 -> 点名 OK")

    # 7 占位文件
    bad7 = base / "bad7"
    build_models(bad7 / "models", files, tiny=("model.onnx",))
    p, _ = m.check_bundle(str(bad7))
    if not any("model.onnx" in x for x in p):
        fails.append(f"占位文件应点名，实际 {p}")
    else:
        print("  [7] 占位文件 -> 点名 OK")

    if fails:
        print("\nFAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("\nPASS 7/7")
    return 0


if __name__ == "__main__":
    sys.exit(main())
