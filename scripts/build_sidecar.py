# -*- coding: utf-8 -*-
"""把 Python 后端打成 Tauri 可托管的 sidecar。

关键约束（均为检验结论，勿改）：
1. 必须用 onedir（不是 onefile）
   —— onefile 的 PID 属于 bootloader，kill 后真实 Python 进程会残留占端口。
2. 必须用 console 模式（不加 --windowed/--noconsole）
   —— --windowed 会丢弃 stdout，导致 Tauri 日志转发与健康检查拿不到输出。
   控制台窗口的隐藏由 Rust 侧 creation_flags(CREATE_NO_WINDOW) 负责。
3. 产物文件名必须带 target triple 后缀，Tauri 的 externalBin 才能找到。
4. _internal/ 必须一并复制到 src-tauri/，并由 tauri.conf.json 的
   resources 携带 —— externalBin 只捆绑单个 exe，不会带上依赖目录。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_TAURI = os.path.join(ROOT, "src-tauri")
BIN_DIR = os.path.join(SRC_TAURI, "binaries")
SIDECAR = "omegaforge-backend"


def target_triple() -> str:
    m = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64",
            "arm64": "aarch64", "aarch64": "aarch64"}.get(m, m)
    s = platform.system()
    if s == "Windows":
        return f"{arch}-pc-windows-msvc"
    if s == "Darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-gnu"


def main() -> int:
    triple = os.environ.get("OF_TARGET_TRIPLE") or target_triple()
    exe_name = f"{SIDECAR}-{triple}{'.exe' if platform.system() == 'Windows' else ''}"

    dist = os.path.join(ROOT, "dist", "sidecar")
    if os.path.isdir(dist):
        shutil.rmtree(dist)

    # 语音运行库：装了就打进包，没装则明确告警。
    #
    # 为什么不能"没装就静默跳过"：静默跳过产出的是"装完没有语音"的安装包，
    # 界面显示未安装、用户得自己去 pip 装——对打包版用户等于没给。而构建
    # 全程是绿的，没有任何一处会指出这一步被跳过了。
    voice_lib = True
    try:
        import importlib.util
        voice_lib = importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        voice_lib = False
    if not voice_lib:
        print("警告：未安装 sherpa-onnx，打包产物将不含语音运行库"
              "（CI 里须先 pip install sherpa-onnx）", file=sys.stderr)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",                 # 见约束 1
        "--console",                # 见约束 2：保留 stdout
        "--noconfirm",
        "--clean",
        "--name", SIDECAR,
        "--distpath", dist,
        "--workpath", os.path.join(ROOT, "build", "sidecar"),
        "--specpath", os.path.join(ROOT, "build"),
        "--paths", ROOT,
        "--exclude-module", "PySide6",      # 桌面壳已换 Tauri，Qt 不再需要
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
    ]
    if voice_lib:
        # sherpa_onnx 带原生动态库，PyInstaller 的默认钩子可能漏收，
        # 漏收时症状是"status 显示已安装、一点识别就报找不到模块"。
        cmd += ["--collect-all", "sherpa_onnx"]
    cmd.append(os.path.join(ROOT, "scripts", "sidecar_main.py"))
    print("$", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print("PyInstaller 失败", file=sys.stderr)
        return r.returncode

    built = os.path.join(dist, SIDECAR)
    src_exe = os.path.join(built, f"{SIDECAR}{'.exe' if platform.system() == 'Windows' else ''}")
    if not os.path.isfile(src_exe):
        print(f"未找到产物 {src_exe}", file=sys.stderr)
        return 1

    os.makedirs(BIN_DIR, exist_ok=True)
    dst_exe = os.path.join(BIN_DIR, exe_name)
    shutil.copy2(src_exe, dst_exe)
    print(f"sidecar -> {dst_exe}")

    # 约束 4：依赖目录必须单独携带
    internal = os.path.join(built, "_internal")
    if os.path.isdir(internal):
        dst_internal = os.path.join(SRC_TAURI, "_internal")
        if os.path.isdir(dst_internal):
            shutil.rmtree(dst_internal)
        shutil.copytree(internal, dst_internal)
        print(f"_internal -> {dst_internal}")
    else:
        print("警告：未发现 _internal，sidecar 可能无法启动", file=sys.stderr)
        return 0

    return bundle_voice(dst_internal)


def bundle_voice(internal: str) -> int:
    """把语音模型下载进 _internal/models，随 resources 一起打包。

    放在复制 _internal 之后：模型目录必须在 resource 里，否则装机后
    voice/assets.py 找不到内置模型，症状与没内置完全相同。

    失败处理：
      OF_VOICE_BUNDLE=0  —— 不内置（仍走界面一键安装）
      OF_VOICE_STRICT=0  —— 下载失败只告警（清单里会留 skipped）
      默认（strict=1）  —— 下载失败让构建失败。静默跳过会产出"装完没
      有语音"的安装包，而构建全程绿，没有任何一处会指出被跳过了。
    """
    if os.environ.get("OF_VOICE_BUNDLE", "1") == "0":
        print("语音模型：按 OF_VOICE_BUNDLE=0 跳过内置")
        return 0

    strict = os.environ.get("OF_VOICE_STRICT", "1") == "1"
    dest = os.path.join(internal, "models")
    cmd = [sys.executable, os.path.join(ROOT, "scripts", "fetch_voice_assets.py"),
           "--dest", dest]
    if not strict:
        cmd.append("--allow-missing")
    print("$", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print(f"警告：语音模型内置失败（rc={r.returncode}）", file=sys.stderr)
        if strict:
            return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
