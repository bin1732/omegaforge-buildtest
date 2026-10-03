"""语音模型获取：多镜像、断点续传、体积校验、原子落盘。

为什么必须有这个模块
--------------------
语音能力（ASR/TTS）的启用路径必须全程在应用内完成：下载、续传、校验、
落盘。要求用户执行安装命令、手工下载文件或手工摆放目录，等于把能力交给
不会用命令行的人去启动——能力真实存在、接口验收全绿，而用户永远启不动。

本模块把它变成一次点击：下载、续传、校验、落盘全部在应用内完成。

## 设计约束（每一条都有对应的失效形态）

1. **镜像必须逐个真试，不能只取第一个的异常就放弃**，也不许跳过整个清单
   静默成功——镜像全挂时报"下载成功"比报错更坏，用户拿到的是 0 字节模型，
   界面显示"可用"，一点就崩。

2. **续传必须先确认服务端真的支持 Range**（HTTP 206）。不发 Range 直接追
   加，服务端忽略 Range 时会把整个文件再发一遍，追加到旧数据后面——文件
   变大、校验通过、内容错乱，属于最难查的一类坏。

3. **落盘必须原子**：先写 .part 再替换。进程在下载中途被杀时，目标路径上
   不能留下半截文件——半截文件过不了体积校验，但会占住路径，让下一次下载
   以为"已经装好了"。

4. **体积校验必须与 asr/tts 的 status() 同一个定义**：统一取
   voice/spec.min_bytes_for（模型文件用 MODEL_MIN_BYTES，词表/词典只要求
   非空）。这里另写一个字面量，会出现"下载器说装好了、status 说没装好"
   的割裂，而两侧单独看都成立。

5. **进度必须落盘，且失败时也落**。前端靠轮询拿进度；只在成功时写的话，
   失败会让界面永久停在"下载中"，与卡死无法区分。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from omegaforge.core.paths import resolve_home
from omegaforge.voice import spec as voice_spec

ProgressCb = Callable[[dict], None]

#: 单文件下载的最长静默时间（秒）。
#: 不给上限时，一个不返回也不关闭连接的服务端会让下载永久挂着，
#: 界面停在进度条上，与"正在下载"无法区分。
TIMEOUT = 30

#: 模型清单（文件、镜像）统一在 voice/spec.py 定义。
#:
#: 为什么不在本模块另写一份：下载清单与运行时需求各写一份时，两份不一致
#: 会让"下载成功"与"仍显示未安装"同时成立，而任一侧单独看都没错——
#: 用户拿到的是装完仍不可用的语音。详见 spec.py 顶部说明。
from omegaforge.voice.spec import SPEC  # noqa: F401  (本模块对外仍以 SPEC 暴露)


def models_root() -> Path:
    """模型根目录：与 asr/tts 的 _models_root() 同一套解析。

    三处各自算路径时，下载器装到 A、status 去 B 找，表现是"下载成功但依然
    不可用"，而两边代码单看都对。
    """
    bundled = Path(__file__).resolve().parent.parent.parent / "models"
    if bundled.is_dir() and any(bundled.iterdir()):
        return bundled
    return Path(resolve_home()) / "models"


def progress_path(kind: str) -> Path:
    return Path(resolve_home()) / f"voice_fetch_{kind}.json"


_LAST: dict = {}
_LOCK = threading.Lock()


def read_progress(kind: str) -> dict:
    p = progress_path(kind)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 读不到不能返回"空闲"——那样前端会把一次正在进行的下载判成没开始，
        # 也可能把一次已失败的下载判成没失败。未知就是未知。
        return {"kind": kind, "state": "unknown", "error":
                "读取下载进度失败，请重试"}


def _write_progress(kind: str, payload: dict) -> None:
    p = progress_path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, p)


def _download_file(url: str, dest: Path, cb: Optional[ProgressCb],
                  state: dict) -> None:
    """下载单个文件，带续传。失败抛 RuntimeError，不静默返回。"""
    part = dest.with_suffix(dest.suffix + ".part")
    resume = part.stat().st_size if part.is_file() else 0

    headers = {"User-Agent": "OmegaForge"}
    mode = "wb"
    if resume > 0:
        headers["Range"] = f"bytes={resume}-"
        mode = "ab"

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        # 必须报出状态码。HTTPError 是 URLError 的子类，只留异常类型名时
        # "地址不存在"(404)、"被拒绝"(403)、"限流"(429) 全都显示成
        # "无法连接（HTTPError）"——排查方向被带到网络/镜像上，而真实原因
        # 可能是仓库名或文件路径写错，重试多少次都不会变。
        snippet = ""
        try:
            snippet = (e.read() or b"").decode("utf-8", "replace")[:80].strip()
        except Exception:
            snippet = ""
        raise RuntimeError(
            f"服务端返回 HTTP {e.code}"
            + (f"（{snippet}）" if snippet else "")) from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"无法连接（{type(e).__name__}）") from e

    with resp:
        # 服务端不支持 Range 时会回 200 并把整个文件重发一遍。追加到旧数据后
        # 面会得到一份更长、能过体积校验、内容错乱的文件，因此必须判 206。
        if resume > 0 and resp.status != 206:
            resume = 0
            mode = "wb"
            part.unlink(missing_ok=True)
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if resume and resp.status == 206:
            total += resume
        got = resume
        chunk = 0
        with open(part, mode) as f:
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                chunk += len(buf)
                if cb and chunk >= 1 << 20:
                    chunk = 0
                    state["bytes_done"] = got
                    cb(dict(state))
        state["bytes_done"] = got

    # 网页内容必须与"文件太小"分开判。词表这类真的小文件（tokens.txt 仅
    # 655 字节）不能套用模型文件的体积下限，于是"镜像返回错误页"这一类
    # 失效就失去了唯一的抓手：错误页体积通常够大、能过体积校验，装好后在
    # onnx 内部才崩，报错完全指不到下载环节。故按内容识别网页。
    head = b""
    try:
        with part.open("rb") as f:
            head = f.read(512)
    except OSError:
        head = b""
    low = head.lower()
    if b"<html" in low or b"<!doctype" in low:
        raise RuntimeError("镜像返回的是网页而不是文件（内容含 HTML 标记）")

    # 下限按条目取（voice/spec.min_bytes_for）：词表/词典这类真的小文件
    # （vits-melo 的 tokens.txt 仅 655 字节）不得套用模型文件的阈值，否则
    # 下载成功却被判"不是有效文件"，而报错看着像网络或镜像的问题。
    floor = voice_spec.min_bytes_for(dest.name)
    if not part.is_file() or part.stat().st_size < floor:
        # 拿回的是错误页 / 占位文件。不许当成成功——它会在 status() 里判为
        # 可用，用户一点合成就崩，而报错来自 onnx 内部，指不到下载环节。
        raise RuntimeError(
            f"下载到的内容不是有效文件（{part.stat().st_size if part.is_file() else 0}"
            f" 字节，小于下限 {floor}）")
    os.replace(part, dest)


def install(kind: str, cb: Optional[ProgressCb] = None) -> dict:
    """下载并安装某类语音模型。同步执行，进度通过 cb 与进度文件外抛。"""
    spec = SPEC.get(kind)
    if not spec:
        raise ValueError(f"未知模型类型：{kind}")

    mirrors = spec["mirrors"]
    if not mirrors:
        # 空清单必须判失败。返回"没有文件要下载 → 成功"会让整条路径恒真：
        # 清单被写空时界面照样报"安装完成"。
        raise RuntimeError(f"{kind} 的镜像清单为空，无法下载")

    root = models_root()
    dest_dir = root / spec["name"]
    dest_dir.mkdir(parents=True, exist_ok=True)

    state = {"kind": kind, "state": "downloading", "model": spec["name"],
             "file": "", "file_index": 0, "file_total": len(spec["files"]),
             "bytes_done": 0, "started": time.time(), "error": ""}
    _write_progress(kind, state)

    last_err = ""
    for idx, fname in enumerate(spec["files"]):
        dest = dest_dir / fname
        if dest.is_file() and dest.stat().st_size >= voice_spec.min_bytes_for(fname):
            state.update(file=fname, file_index=idx + 1)
            _write_progress(kind, state)
            continue
        ok = False
        # 逐个镜像的原因都要留下来。只留最后一次时，前面的失败（例如仓库
        # 名写错 404）被后面的失败（例如另一个镜像限流 429）覆盖，日志里
        # 只剩最不相关的那一条。
        errs: list = []
        for mirror in mirrors:
            state.update(file=fname, file_index=idx, mirror=mirror)
            _write_progress(kind, state)
            try:
                _download_file(f"{mirror}/{fname}", dest, cb, state)
                ok = True
                break
            except (RuntimeError, OSError) as e:
                errs.append(f"{mirror} → {e}")
        last_err = f"{fname}：" + "；".join(errs)
        if not ok:
            # 失败也必须落盘并带上真实原因：只在成功时写进度，前端会永久停
            # 在"下载中"，而真实原因（镜像地址错/网络不通/内容不是模型）
            # 全被吞掉。
            state.update(state="failed", error=last_err or "所有镜像均下载失败")
            _write_progress(kind, state)
            raise RuntimeError(last_err or "所有镜像均下载失败")

    state.update(state="done", error="", file_index=len(spec["files"]))
    _write_progress(kind, state)
    with _LOCK:
        _LAST[kind] = dict(state)
    return dict(state)


_THREADS: dict = {}


def start(kind: str) -> dict:
    """在后台线程里开始下载，立即返回。

    为什么不直接在请求里同步下载：模型是几十到几百 MB，同步下载会让 HTTP
    请求挂在那里直到客户端超时——界面表现为"点了没反应"，而下载其实还在跑；
    更糟的是重复点击会起第二个下载，两个线程往同一个 .part 写，产出的文件
    互相覆盖、体积也可能够大，校验通过但内容错乱。

    因此这里只负责起线程并做去重，真实进度由 read_progress() 轮询。
    """
    spec = SPEC.get(kind)
    if not spec:
        raise ValueError(f"未知模型类型：{kind}")

    with _LOCK:
        t = _THREADS.get(kind)
        if t is not None and t.is_alive():
            return {"started": False, "reason": "already_running",
                    "progress": read_progress(kind)}
        cur = read_progress(kind)
        if cur.get("state") == "done" and not _missing_files(kind):
            return {"started": False, "reason": "already_installed",
                    "progress": cur}

        holder: dict = {}

        def _run() -> None:
            try:
                install(kind, cb=lambda st: holder.update(st))
            except (RuntimeError, OSError, ValueError) as e:
                # 线程里的异常不会传播到任何请求；不落盘的话前端会停在
                # "下载中"，与卡死无法区分。
                st = read_progress(kind)
                st.update(state="failed",
                          error=str(e) or "下载失败")
                _write_progress(kind, st)

        th = threading.Thread(target=_run, name=f"voice-fetch-{kind}",
                              daemon=True)
        _THREADS[kind] = th
        th.start()
    return {"started": True, "reason": "", "progress": read_progress(kind)}


def _missing_files(kind: str) -> list:
    spec = SPEC[kind]
    d = models_root() / spec["name"]
    return [f for f in spec["files"]
            if not (d / f).is_file()
            or (d / f).stat().st_size < voice_spec.min_bytes_for(f)]
