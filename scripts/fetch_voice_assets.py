# -*- coding: utf-8 -*-
"""CI 打包前内置语音资源：运行库与模型。

## 为什么要有这一步

语音能力的启用门槛必须全部在打包阶段消除：
  1. pip 装 sherpa-onnx（不会用命令行的人做不到）
  2. 从浏览器手工下载模型（几十到几百 MB，需要知道下哪个）
  3. 手工把文件摆到正确目录（需要知道数据目录在哪）

任何一道留在用户侧，结果都是：能力真实存在、接口验收全绿，而用户永远
启不动。本脚本把三道全部挪到打包阶段，产出开箱即用的安装包。

## 设计约束（每条都对应一种具体失效）

1. **镜像地址可通过环境变量覆盖**。内网/私有镜像是真实需求；同时它让本
   脚本在沙盒里能对着本地 HTTP 服务端端到端验证，而不是靠断言网络可达。

2. **下载失败必须让构建失败，并指名哪个文件、哪个镜像**。静默跳过会产出
   "装完没有语音"的安装包——界面显示未安装，用户又回到三道门槛上，而
   构建全程是绿的。

3. **必须写清单（manifest）**。没有清单时，"内置了什么、多大、来自哪个
   镜像"全靠猜；打包步骤与装机验收两侧无法对账，内置成功与否无从判定。

4. **体积下限必须与运行时同一定义**（voice/spec.min_bytes_for）。这里另写
   常量会出现"打包说装好了、运行时说缺失"；对所有条目套用模型文件的阈值
   则会把词表这类真的小文件（655 字节）判成产物过小。

5. **跳过（--allow-missing）也要留痕**：清单里写明 skipped 与原因。不留
   痕的话，构建绿、装机后没语音，排查时连"到底下载过没有"都查不到。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.voice import fetch  # noqa: E402
from omegaforge.voice import spec as voice_spec  # noqa: E402

#: 环境变量覆盖镜像（逗号分隔），用于内网镜像与离线验证。
ENV_ASR = "OF_VOICE_MIRROR_ASR"
ENV_TTS = "OF_VOICE_MIRROR_TTS"

MANIFEST_NAME = "voice_manifest.json"


def _mirrors(kind: str) -> list:
    env = ENV_ASR if kind == "asr" else ENV_TTS
    raw = os.environ.get(env, "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return list(fetch.SPEC[kind]["mirrors"])


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_kind(kind: str, dest: Path) -> dict:
    """下载一类模型到 dest/<model_name>/，返回清单条目。"""
    spec = fetch.SPEC[kind]
    mirrors = _mirrors(kind)
    if not mirrors:
        raise RuntimeError(f"{kind} 镜像清单为空（{ENV_ASR if kind == 'asr' else ENV_TTS} 未设置且内置清单为空）")

    model_dir = dest / spec["name"]
    model_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for fname in spec["files"]:
        target = model_dir / fname
        # 清单里含子目录路径（如 dict/jieba.dict.utf8）：父目录必须先建，
        # 否则写文件时报"目录不存在"，而报错看不出缺的是哪一层。
        target.parent.mkdir(parents=True, exist_ok=True)
        # 逐个镜像的原因都要留下来：只留最后一次时，前面的失败原因（例如
        # 仓库名写错 404）会被后面的失败（例如限流 429）覆盖，日志里只剩
        # 最不相关的那一条。
        errs: list = []
        done = False
        # 下限按条目取：词表/词典是真的小文件（vits-melo 的 tokens.txt 仅
        # 655 字节），套用模型文件的下限会让"下载成功"被判成产物过小。
        floor = voice_spec.min_bytes_for(fname)
        for mirror in mirrors:
            url = f"{mirror}/{fname}"
            try:
                fetch._download_file(url, target, None, {})
                if not target.is_file() or target.stat().st_size < floor:
                    raise RuntimeError(
                        f"产物过小（{target.stat().st_size if target.is_file() else 0} 字节，"
                        f"下限 {floor}）——镜像可能返回了网页")
                done = True
                files.append({"name": fname, "bytes": target.stat().st_size,
                              "sha256": _sha256(target), "mirror": mirror})
                break
            except (RuntimeError, OSError) as e:
                errs.append(f"从 {mirror} 下载失败：{e}")
        if not done:
            # 见约束 2：指名文件与镜像，且让构建失败。
            detail = "；".join(errs) if errs else "所有镜像均失败"
            raise RuntimeError(f"{fname} {detail}")

    # 下载后自检：按运行时需求条目复核，而不是只数自己下了几个文件。
    #
    # 没有这一步时，清单不完整（缺 lexicon.txt / dict/）会让下载"成功"、
    # 而 status 仍报缺失——用户装完了仍用不了，两边却都没有错。
    uncovered = voice_spec.uncovered_requirements(kind)
    if uncovered:
        # 清单没覆盖需求时，逐项复核会通过（缺的根本没被检查）——
        # 必须先判覆盖，否则自检与"下载成功"没有区别。
        raise RuntimeError(
            f"{spec['name']} 的下载清单未覆盖运行时需求：{'、'.join(uncovered)}")

    missing = []
    for entry in voice_spec.required_entries(kind):
        p = model_dir / entry
        ok = False
        if voice_spec.is_dir_entry(kind, entry):
            ok = p.is_dir() and any(q.is_file() for q in p.rglob("*"))
        else:
            ok = (p.is_file()
                  and p.stat().st_size >= voice_spec.min_bytes_for(entry))
        if not ok:
            missing.append(entry)
    if missing:
        raise RuntimeError(
            f"{spec['name']} 下载完成但运行时需求未满足，缺：{'、'.join(missing)}"
            "——下载清单与需求清单已同源，出现此项说明某个文件下载成了占位内容")

    return {"kind": kind, "model": spec["name"], "files": files,
            "bytes": sum(f["bytes"] for f in files), "required_ok": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="模型根目录（其下建 <model_name>/）")
    ap.add_argument("--only", default="asr,tts", help="要内置的模型类型，逗号分隔")
    ap.add_argument("--allow-missing", action="store_true",
                    help="下载失败时不让构建失败（仍会写 skipped 清单）")
    args = ap.parse_args()

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    manifest = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "dest": str(dest), "models": [], "skipped": []}

    rc = 0
    for kind in [k.strip() for k in args.only.split(",") if k.strip()]:
        try:
            entry = fetch_kind(kind, dest)
            manifest["models"].append(entry)
            print(f"[voice-assets] {kind}: {entry['bytes']} 字节 / "
                  f"{len(entry['files'])} 个文件 -> {dest / entry['model']}")
        except (RuntimeError, OSError) as e:
            # 约束 5：跳过也留痕。
            manifest["skipped"].append({"kind": kind, "reason": str(e)})
            if args.allow_missing:
                print(f"[voice-assets] 跳过 {kind}：{e}", file=sys.stderr)
                rc = 0
            else:
                print(f"[voice-assets] 失败 {kind}：{e}", file=sys.stderr)
                rc = 1

    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[voice-assets] 清单 -> {dest / MANIFEST_NAME}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
