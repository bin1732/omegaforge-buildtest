"""OmegaForge Voice TTS — 内置 sherpa-onnx OfflineTts（vits-melo-tts-zh_en）。

中英双语离线合成，CPU 实时。模型随安装包内置（resources/models），
数据目录环境变量 OMEGAFORGE_HOME 下亦可。输出 16bit WAV base64。
"""
from __future__ import annotations

import base64
import importlib.util
import io
import os
import struct
import time
import wave
from pathlib import Path
from typing import Optional

from omegaforge.core.errors import UserError
from omegaforge.core.paths import resolve_home
from omegaforge.voice import assets
from omegaforge.voice import spec as voice_spec

TTS_MODEL_NAME = "vits-melo-tts-zh-en"
TTS_MODEL_FILE = "model.onnx"               # fp32（int8 占位文件不可用）

#: 体积下限见 model_size.py：asr 与 tts 必须取同一个定义。
#: 小于该体积的 model.onnx（0 字节）与 tokens.txt（100 字节）会被 exists()
#: 判为齐备，files_missing 为空、界面显示"可用"，用户一点合成就崩。


def _is_real(p: Path, entry: str = "") -> bool:
    """存在且不是占位产物。

    目录（dict）按"是否含至少一个文件"判断 —— 词典目录里是一堆小文件，
    逐个套用 10KB 阈值会误杀正常安装。

    单文件按条目的下限判断（voice/spec.min_bytes_for）：模型文件用
    MODEL_MIN_BYTES，词表/词典只要求非空——vits-melo 的 tokens.txt 只有
    655 字节，统一套用模型文件的阈值会让文件在却被判缺失，界面显示
    "模型未下载完整"，用户点合成永远失败。
    """
    try:
        if p.is_dir():
            return any(f.is_file() for f in p.rglob("*"))
        return p.is_file() and p.stat().st_size >= voice_spec.min_bytes_for(
            entry or p.name)
    except OSError:
        return False


def _models_root() -> Path:
    """内置路径（随包）优先，其次数据目录 models/。

    同 asr：解析逻辑统一在 voice/assets.py，避免打包后 __file__ 层级推导
    落到 _internal/models 而实际资源在安装目录根/models。
    """
    return assets.resolve(TTS_MODEL_NAME)[0]


def models_source() -> str:
    """当前解析到的模型来自哪（bundled / source / home），用于诊断。"""
    return assets.resolve(TTS_MODEL_NAME)[1]


class TtsEngine:
    def __init__(self, home: Optional[str] = None):
        self.home = Path(home) if home else _models_root()
        self._tts = None
        self._sid = None
        self._error = ""

    def status(self) -> dict:
        mdir = self.home / TTS_MODEL_NAME
        # 需求清单取自 voice/spec.py，与下载清单同源：两边各写一份时
        # 不一致会以"下载成功但仍显示未安装"的形式留下来。
        need = voice_spec.required_entries("tts")
        missing = [f for f in need if not _is_real(mdir / f, f)]
        has_lib = importlib.util.find_spec("sherpa_onnx") is not None
        return {"model": TTS_MODEL_NAME, "dir": str(mdir),
                "files_missing": missing, "lib_installed": has_lib,
                "ready": has_lib and not missing, "error": self._error}

    def _ensure(self) -> None:
        if self._tts is not None:
            return
        # 同 asr：未安装/模型缺失是可自助修复的前提，不是服务器故障。
        # POST /api/voice/tts 恒 500「操作失败，请稍后重试」，
        # 而同一时刻 GET /api/voice/status 明确报 ready=false 与缺失清单。
        if importlib.util.find_spec("sherpa_onnx") is None:
            raise UserError(
                "语音合成依赖未安装：请在设置页的语音面板按提示安装后重试")
        import sherpa_onnx
        mdir = self.home / TTS_MODEL_NAME
        if not (mdir / TTS_MODEL_FILE).is_file():
            missing = [f for f in voice_spec.required_entries("tts")
                       if not _is_real(mdir / f, f)]
            self._error = "TTS 模型未下载完整"
            raise UserError(
                "语音合成模型未下载完整，缺少：" + "、".join(missing) +
                "。请在设置页的语音面板查看模型下载来源")
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(mdir / TTS_MODEL_FILE),
            lexicon=str(mdir / "lexicon.txt"),
            tokens=str(mdir / "tokens.txt"),
            dict_dir=str(mdir / "dict"),
            noise_scale=0.667, noise_scale_w=0.8, length_scale=1.0)
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=2))
        self._tts = sherpa_onnx.OfflineTts(cfg)

    def synthesize(self, text: str, sid: int = 0,
                   speed: float = 1.0) -> dict:
        # 例如：HTTP 路由上 need("text") 会先拦，所以这条**在 HTTP 上不可达**，
        # 属潜伏分支而非活跃缺陷。但仍要改，理由与相关部分一致：
        #   · ValueError 经 errors.py 归类后落到「请求内容有误，请检查后重试」，
        #     这句英文 "text required" 根本到不了用户眼前（原样外泄则是英文）；
        #   · 一旦将来出现非 HTTP 调用方（MCP / 内部编排），它就会变成
        #     "报错了但不知道缺什么"的面孔。
        # 改成 UserError + 中文，成本为零，且与 asr.py 同源分支口径一致。
        if not text or not text.strip():
            raise UserError("要朗读的文字不能为空")
        self._ensure()
        t0 = time.time()
        audio = self._tts.generate(text, sid=sid, speed=max(0.5, min(2.0, speed)))
        samples = audio.samples
        sr = audio.sample_rate
        pcm = struct.pack(f"<{len(samples)}h",
                          *(max(-32768, min(32767, int(s * 32767)))
                            for s in samples))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)
        dur = len(samples) / sr
        return {"audio_b64": base64.b64encode(buf.getvalue()).decode(),
                "sample_rate": sr, "duration_s": round(dur, 2),
                "latency_ms": int((time.time() - t0) * 1000),
                "rtf": round((time.time() - t0) / max(dur, 0.01), 3)}


def engine(home: Optional[str] = None) -> TtsEngine:
    return TtsEngine(home)
