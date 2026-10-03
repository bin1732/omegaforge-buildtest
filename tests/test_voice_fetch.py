"""语音模型下载：对真实 HTTP 服务端跑通，不靠 mock 假装。

为什么不用 mock
--------------
下载链路的失效几乎全在网络与文件边界上：服务端不支持 Range、返回的是错误
页而不是模型、续传时把整份重发、镜像全挂。用 mock 把这些都替换掉，测的是
代码流程，不是下载。本机起一个真的 HTTP 服务端（支持/不支持 Range 各一种）
才能真正验到。

本机 localhost 可用（外网在本沙盒被拦），所以这是真实端到端。
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
from pathlib import Path

import pytest

from omegaforge.voice import fetch
from omegaforge.voice.model_size import MODEL_MIN_BYTES

BODY = b"\x00" * (MODEL_MIN_BYTES + 4096)


class _Handler(http.server.BaseHTTPRequestHandler):
    """可切换行为的服务端：正常 / 忽略 Range / 返回错误页。"""
    mode = "ok"
    hits = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, extra=None, start=0):
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body) - start))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body[start:])

    def do_GET(self):
        type(self).hits.append(self.path)
        rng = self.headers.get("Range")
        if self.mode == "html":
            self._send(200, b"<html><body>404 not found</body></html>" * 10)
            return
        if self.mode == "tiny":
            self._send(200, b"x" * 10)
            return
        if self.mode == "small_text":
            # 真实词表体积：vits-melo 的 tokens.txt 为 655 字节；
            # 模型文件仍返回正常体积，好让"只拒模型"的断言成立。
            if "tokens.txt" in self.path:
                self._send(200, b"a a\nb b\n" + b"x" * (655 - 8))
            else:
                self._send(200, BODY)
            return
        if rng and self.mode == "ok":
            # 真正的续传：206 + 只发剩余部分
            start = int(rng.split("=")[1].split("-")[0])
            self._send(206, BODY, {"Content-Range":
                                   f"bytes {start}-{len(BODY)-1}/{len(BODY)}"},
                       start=start)
            return
        if rng and self.mode == "ignore_range":
            # 忽略 Range：回 200 并发整份。客户端若照单追加，会得到一份
            # 更长、能过体积校验、内容错乱的文件。
            self._send(200, BODY)
            return
        self._send(200, BODY)


def _serve(mode):
    _Handler.mode = mode
    _Handler.hits = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))
    # 避免读到仓库内 models/ 内置目录而跳过数据目录
    monkeypatch.setattr(fetch, "models_root", lambda: tmp_path / "models")
    monkeypatch.setattr(fetch, "progress_path",
                        lambda k: tmp_path / f"prog_{k}.json")
    return tmp_path


def _spec(monkeypatch, urls):
    monkeypatch.setitem(fetch.SPEC, "asr", {
        "name": "asr-test", "mirrors": list(urls),
        "files": ["tokens.txt", "encoder.onnx"]})


def test_download_installs_real_files(home, monkeypatch):
    srv, url = _serve("ok")
    try:
        _spec(monkeypatch, [url])
        st = fetch.install("asr")
        assert st["state"] == "done"
        d = home / "models" / "asr-test"
        assert (d / "tokens.txt").stat().st_size == len(BODY)
        assert (d / "encoder.onnx").stat().st_size == len(BODY)
        # 原子落盘：不得留下 .part
        assert list(d.glob("*.part")) == []
    finally:
        srv.shutdown()


def test_resume_uses_range(home, monkeypatch):
    srv, url = _serve("ok")
    try:
        _spec(monkeypatch, [url])
        d = home / "models" / "asr-test"
        d.mkdir(parents=True)
        # 预置半截 .part
        (d / "tokens.txt.part").write_bytes(BODY[:1000])
        fetch.install("asr")
        assert (d / "tokens.txt").stat().st_size == len(BODY), (
            "续传后文件必须完整：服务端回了 206 却没补齐，或没发 Range")
    finally:
        srv.shutdown()


def test_server_ignoring_range_must_not_corrupt(home, monkeypatch):
    """服务端忽略 Range 时，追加会得到错乱文件——必须重头下。"""
    srv, url = _serve("ignore_range")
    try:
        _spec(monkeypatch, [url])
        d = home / "models" / "asr-test"
        d.mkdir(parents=True)
        (d / "tokens.txt.part").write_bytes(BODY[:1000])
        fetch.install("asr")
        size = (d / "tokens.txt").stat().st_size
        assert size == len(BODY), (
            f"服务端回 200 却仍被当续传：文件 {size} 字节（应为 {len(BODY)}）")
    finally:
        srv.shutdown()


def test_error_page_rejected(home, monkeypatch):
    """镜像返回网页时不得当成模型装好。

    网页的体积通常够大（HTML 页几 KB 起步），而过不了体积校验的只有小文件。
    因此这里必须靠内容识别，不能只靠体积——否则错误页会被当成词表装好，
    装完在 onnx 内部才崩，报错完全指不到下载环节。
    """
    srv, url = _serve("html")
    try:
        _spec(monkeypatch, [url])
        with pytest.raises(RuntimeError) as ei:
            fetch.install("asr")
        assert "网页" in str(ei.value)
        assert not (home / "models" / "asr-test" / "tokens.txt").exists()
        # 失败必须落盘，否则界面永久停在"下载中"
        pr = fetch.read_progress("asr")
        assert pr["state"] == "failed" and pr["error"]
    finally:
        srv.shutdown()


def test_tiny_model_rejected(home, monkeypatch):
    """截断/占位的模型文件必须被拒，且不得落盘为已装好。"""
    srv, url = _serve("tiny")
    try:
        _spec(monkeypatch, [url])
        with pytest.raises(RuntimeError):
            fetch.install("asr")
        assert not (home / "models" / "asr-test" / "encoder.onnx").exists()
    finally:
        srv.shutdown()


def test_small_vocab_accepted(home, monkeypatch):
    """词表是小文件：不得被模型文件的体积下限误杀。

    vits-melo 的 tokens.txt 只有 655 字节。对所有条目统一套用模型文件的
    下限，结果是"下载成功却判不是有效文件"——打包内置阶段因此构建失败，
    运行时则报文件缺失、界面显示未安装。
    """
    srv, url = _serve("small_text")
    try:
        _spec(monkeypatch, [url])
        st = fetch.install("asr")
        assert st["state"] == "done"
        d = home / "models" / "asr-test"
        assert (d / "tokens.txt").stat().st_size == 655
    finally:
        srv.shutdown()


def test_mirror_fallback(home, monkeypatch):
    """第一个镜像不可用时必须换下一个，而不是直接失败。"""
    good, gurl = _serve("ok")
    try:
        dead = "http://127.0.0.1:1"   # 必然连不上
        _spec(monkeypatch, [dead, gurl])
        st = fetch.install("asr")
        assert st["state"] == "done"
    finally:
        good.shutdown()


def test_all_mirrors_dead_fails_loudly(home, monkeypatch):
    _spec(monkeypatch, ["http://127.0.0.1:1", "http://127.0.0.1:2"])
    with pytest.raises(RuntimeError) as ei:
        fetch.install("asr")
    assert "所有镜像均下载失败" in str(ei.value) or "无法连接" in str(ei.value)


def test_empty_mirror_list_is_failure(home, monkeypatch):
    """空清单必须判失败：返回成功会让整条安装路径恒真。"""
    _spec(monkeypatch, [])
    with pytest.raises(RuntimeError) as ei:
        fetch.install("asr")
    assert "镜像清单为空" in str(ei.value)


def test_progress_unknown_when_absent(home):
    """读不到进度不得返回"空闲"——会把进行中/已失败判成没开始。"""
    pr = fetch.read_progress("asr")
    assert pr["state"] == "unknown" and pr["error"]


def test_start_does_not_double_download(home, monkeypatch):
    srv, url = _serve("ok")
    try:
        _spec(monkeypatch, [url])
        r1 = fetch.start("asr")
        assert r1["started"] is True
        r2 = fetch.start("asr")
        # 第二次不能起第二个下载：两线程写同一 .part 会互相覆盖
        assert r2["started"] is False
        for t in list(fetch._THREADS.values()):
            t.join(timeout=30)
    finally:
        srv.shutdown()
