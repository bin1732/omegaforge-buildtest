# -*- coding: utf-8 -*-
"""验证内置语音资源下载器：对真实 HTTP 服务端端到端跑。

沙盒外网不可达（huggingface / hf-mirror / pypi 均 403），因此用本机真实
服务端验证下载逻辑本身；镜像地址通过环境变量注入，这也是内网镜像的真实
用法。

被验证的失效形态：
  1. 正常下载 -> 落盘 + 清单
  2. 镜像返回 HTML 错误页 -> 必须失败并指名文件与镜像
  3. 404 -> 必须失败
  4. 产物过小 -> 必须失败（体积下限与运行时同一定义）
  5. 镜像清单为空 -> 必须失败（返回"没文件要下→成功"会让整条路径恒真）
  6. --allow-missing -> 跳过但清单里必须留痕
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fetch_voice_assets.py"

MODELS = {
    "asr": ["tokens.txt", "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.int8.onnx"],
    "tts": ["model.onnx", "tokens.txt", "lexicon.txt",
            "dict/jieba.dict.utf8", "dict/hmm_model.utf8",
            "dict/user.dict.utf8", "dict/idf.utf8", "dict/stop_words.utf8"],
}


class Handler(http.server.BaseHTTPRequestHandler):
    root = None
    mode = "ok"

    def do_GET(self):  # noqa: N802
        name = self.path.lstrip("/")
        if self.mode == "html":
            body = b"<html><body>404 not found on this mirror</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.mode == "tiny":
            body = b"x" * 10
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        p = self.root / name
        if not p.is_file():
            self.send_error(404)
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def serve(root: Path, mode: str = "ok"):
    Handler.root = root
    Handler.mode = mode
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def run(dest: Path, asr: str = "", tts: str = "", extra=()):
    env = dict(os.environ)
    if asr:
        env["OF_VOICE_MIRROR_ASR"] = asr
    if tts:
        env["OF_VOICE_MIRROR_TTS"] = tts
    cmd = [sys.executable, str(SCRIPT), "--dest", str(dest), *extra]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       cwd=str(ROOT), timeout=120)
    return r


def main() -> int:
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="vassets-"))
    src = base / "src"
    src.mkdir()
    for names in MODELS.values():
        for n in names:
            f = src / n
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"M" * 120_000)

    fails = []

    # 1. 正常下载
    srv, url = serve(src)
    dest = base / "d1"
    try:
        r = run(dest, url, url)
        if r.returncode != 0:
            fails.append(f"正常下载应成功：rc={r.returncode} {r.stderr[-300:]}")
        else:
            man = json.loads((dest / "voice_manifest.json").read_text())
            kinds = sorted(m["kind"] for m in man["models"])
            if kinds != ["asr", "tts"]:
                fails.append(f"清单应含 asr/tts，实际 {kinds}")
            if man["skipped"]:
                fails.append(f"清单不应有 skipped：{man['skipped']}")
            got = sorted(p.name for p in (dest / "asr-zipformer-zh-en").iterdir())
            if got != sorted(MODELS["asr"]):
                fails.append(f"asr 落盘文件不符：{got}")
            total = sum(m["bytes"] for m in man["models"])
            print(f"  [1] 正常下载 OK：{total} 字节，清单 {len(man['models'])} 项")
    finally:
        srv.shutdown()

    # 2. 镜像返回 HTML
    srv2, url2 = serve(src, mode="html")
    try:
        r = run(base / "d2", url2, url2)
        if r.returncode == 0:
            fails.append("HTML 错误页应让构建失败")
        else:
            if "tokens.txt" not in r.stderr and "model.onnx" not in r.stderr:
                fails.append(f"报错未指名文件：{r.stderr[-200:]}")
            print("  [2] HTML 错误页 -> 失败并指名文件 OK")
    finally:
        srv2.shutdown()

    # 3. 缺失文件 -> 404 必须失败
    empty = base / "empty"
    empty.mkdir()
    srv3, url3 = serve(empty)
    try:
        r = run(base / "d3", url3, url3)
        if r.returncode == 0:
            fails.append("404 应让构建失败")
        else:
            print("  [3] 404 -> 失败 OK")
    finally:
        srv3.shutdown()

    # 4. 产物过小
    srv4, url4 = serve(src, mode="tiny")
    try:
        r = run(base / "d4", url4, url4)
        if r.returncode == 0:
            fails.append("过小产物应让构建失败")
        else:
            # 体积判据在 fetch._download_file 与本脚本两处，措辞可能不同；
            # 共同点是报错里带真实字节数 —— 说明判的是体积而不是网络状态。
            if "10 字节" not in r.stderr and "10字节" not in r.stderr:
                fails.append(f"报错未给出真实字节数：{r.stderr[-200:]}")
            print("  [4] 过小产物 -> 失败并给出字节数 OK")
    finally:
        srv4.shutdown()

    # 5. 镜像清单为空
    # 子进程会重新导入内置清单，无法从外部清空，因此直接在进程内调用
    # fetch_kind 验证：清单为空时必须抛错，而不是"没有文件要下 -> 成功"。
    sys.path.insert(0, str(ROOT))
    import omegaforge.voice.fetch as fetchmod
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "fetch_voice_assets", str(SCRIPT))
    assets_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(assets_mod)
    saved = {k: list(v["mirrors"]) for k, v in fetchmod.SPEC.items()}
    try:
        for v in fetchmod.SPEC.values():
            v["mirrors"] = []
        try:
            assets_mod.fetch_kind("asr", base / "d5")
            fails.append("空镜像清单应抛错（返回成功会让整条路径恒真）")
        except RuntimeError as e:
            if "镜像清单为空" not in str(e):
                fails.append(f"空清单报错未说明原因：{e}")
            else:
                print("  [5] 空镜像清单 -> 失败并说明原因 OK")
    finally:
        for k, v in saved.items():
            fetchmod.SPEC[k]["mirrors"] = v

    # 6. --allow-missing 跳过但留痕
    srv6, url6 = serve(empty)
    try:
        d6 = base / "d6"
        r = run(d6, url6, url6, extra=("--allow-missing",))
        if r.returncode != 0:
            fails.append(f"--allow-missing 应不失败：{r.stderr[-200:]}")
        else:
            man = json.loads((d6 / "voice_manifest.json").read_text())
            if not man["skipped"]:
                fails.append("--allow-missing 跳过时清单必须留痕")
            else:
                print(f"  [6] 跳过留痕 OK：{len(man['skipped'])} 条")
    finally:
        srv6.shutdown()

    # 7. 清单不完整（回到只有 model.onnx + tokens.txt 的旧形态）
    #    下载会"成功"，但运行时需求（lexicon.txt / dict）未满足 —— 必须被
    #    自检拦住。这是"装完了仍显示未安装"那一类病唯一的防线。
    # 子进程会重新导入模块，父进程改清单对子进程无效，因此在进程内调用。
    import importlib.util
    _s7 = importlib.util.spec_from_file_location("fva7", str(SCRIPT))
    m7 = importlib.util.module_from_spec(_s7)
    _s7.loader.exec_module(m7)
    sys.path.insert(0, str(ROOT))
    import omegaforge.voice.fetch as vf
    saved_tts = list(vf.SPEC["tts"]["files"])
    srv7, url7 = serve(src)
    try:
        vf.SPEC["tts"]["files"] = ["model.onnx", "tokens.txt"]
        os.environ["OF_VOICE_MIRROR_TTS"] = url7
        try:
            m7.fetch_kind("tts", base / "d7")
            fails.append("清单不完整时应被自检拦住（下载成功但仍不可用）")
        except RuntimeError as e:
            if "未覆盖运行时需求" not in str(e):
                fails.append(f"自检未说明缺什么：{e}")
            else:
                print(f"  [7] 清单不完整 -> 自检拦住 OK：{str(e)[-60:]}")
    finally:
        vf.SPEC["tts"]["files"] = saved_tts
        os.environ.pop("OF_VOICE_MIRROR_TTS", None)
        srv7.shutdown()

    if fails:
        print("\nFAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("\nPASS 6/6")
    return 0


if __name__ == "__main__":
    sys.exit(main())
