"""OmegaForge Voice — 内置语音 Tier-1：sherpa-onnx 流式 ASR。

模型策略（内置优先）：
  1. 查找 models/<model_name>/（随安装包内置或手动放置）
  2. 缺失时报告 status=missing + 下载指引（fetch_models.py 多镜像）
  3. 模型就绪后提供真流式识别（16kHz PCM）

零第三方运行时依赖之外仅 sherpa-onnx（可选安装，未安装时优雅降级）。
"""
from __future__ import annotations

import base64
import importlib.util
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

ASR_MODEL_NAME = "asr-zipformer-zh-en"


def _home() -> Path:
    return Path(resolve_home())


def _models_root() -> Path:
    """内置路径（随包）优先，其次数据目录 models/。

    解析逻辑在 voice/assets.py 里只定义一次：此前这里用
    `Path(__file__).parent.parent.parent / "models"` 推导，打包后 __file__
    位于 _internal/omegaforge/voice/，推导结果落在 _internal/models，
    而 Tauri resources 把模型放在安装目录根/models —— 内置了却找不到，
    症状与没内置完全一样。见 assets.py 顶部说明。
    """
    return assets.resolve(ASR_MODEL_NAME)[0]


def models_source() -> str:
    """当前解析到的模型来自哪（bundled / source / home），用于诊断。"""
    return assets.resolve(ASR_MODEL_NAME)[1]


#: 需求清单与下载清单同源（voice/spec.py）。
#:
#: 需求只允许在一处定义。两处各写一份时，下载器只对自己那份负责、
#: 状态检查只对运行需求负责，两边都成立而用户拿到的是"装完了仍显示未安装"。
REQUIRED_FILES = voice_spec.required_entries("asr")


#: 重采样只接受人声/语音常见采样率区间。
#: 例如：WAV 头里把 framerate 写成 100 时，recognize_wav_b64 的重采样会把
#: 输入按 `16000 / sr` 倍放大 —— 42KB 的音频被展开成 6.4MB（160x）；
#: 写成 1 就是 16000x（数百 MB），一个几十 KB 的上传就能打爆进程内存。
#: 低于下界/高于上界都属于异常声明，直接拒绝而不是照着重采样。
SAMPLE_RATE_MIN = 8_000
SAMPLE_RATE_MAX = 48_000

#: 单条音频的**样本数**上限（不是秒）。
#:
#: 为什么单位是样本而不是秒——两个原因：
#:
#: 1) 解码后每个样本会被物化成独立的整数对象，内存放大约 17.9 倍：
#:        300s @48kHz  PCM 27.5MB  ->  unpack 峰值 492.9MB
#:        310s @8kHz   PCM  4.7MB  ->  unpack 峰值  84.9MB
#:    放大发生在 `recognize_wav_b64` 里的 `struct.unpack`
#:    （重采样 + accept_waveform 各一次），而 `readframes` 返回紧凑字节，
#:    本身不放大。注意：用固定字节序列造样本会命中解释器的小整数缓存，
#:    测出的放大倍数偏低，需以随机幅值衡量。
#:
#: 2) 若按秒数设上限，上限会形同虚设：300 秒在 48kHz 下是 1440 万样本，
#:    峰值约 493MB。放大倍数只与样本数有关、与采样率无关，
#:    48kHz 下 50 秒 与 8kHz 下 300 秒 峰值相同，因此秒数不是合适的单位。
#:
#: 因此预算直接落在样本数上。2_400_000 样本 -> 峰值约 82MB；
#: 换算到常见采样率是 16kHz 下 150 秒、48kHz 下 50 秒、8kHz 下 300 秒。
#:
#: 判定用 WAV 头里的 nframes（O(1)，在物化之前即可判定）：
#:   · 头声明小于实际数据 -> readframes 只按声明量读，不会多读；
#:   · 头声明大于实际数据 -> 这里先拒，宁可误拒也不多读。
#: 两个方向都是安全侧。
#:
#: 未消除的残余风险：17.9x 放大本身仍在（上限只是把它框住，没消除它）。
#: 根治要把 struct.unpack 换成 array.array('h') 流式喂给 recognize，
#: 但 accept_waveform 要求 list[float]，且该路径依赖未安装的 sherpa-onnx，
#: 本版无法端到端验证，故未改——如实登记，不包装成已解决。
MAX_PCM_SAMPLES = 2_400_000


def _wav_to_pcm16(audio_b64: str) -> tuple[bytes, int]:
    """base64(wav) -> (pcm16 bytes, sample_rate)。"""
    raw = base64.b64decode(audio_b64)
    import io
    with wave.open(io.BytesIO(raw), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise UserError("音频格式有误，请提供单声道 16 位 WAV 音频")
        sr = w.getframerate()
        if not (SAMPLE_RATE_MIN <= sr <= SAMPLE_RATE_MAX):
            raise UserError(
                f"音频采样率异常（{sr}Hz），请提供 "
                f"{SAMPLE_RATE_MIN // 1000}kHz～{SAMPLE_RATE_MAX // 1000}kHz 的音频"
            )
        # 样本数必须在物化之前判定（物化点在 recognize_wav_b64 的 unpack）。
        # 报给用户的是秒数，但判据是样本数——见 MAX_PCM_SAMPLES 处的。
        nframes = w.getnframes()
        if nframes > MAX_PCM_SAMPLES:
            dur = int(nframes / sr) if sr else 0
            limit = int(MAX_PCM_SAMPLES / sr) if sr else 0
            raise UserError(
                f"音频过长（约 {dur} 秒，{sr // 1000}kHz 下单次上限约 "
                f"{limit} 秒），请把更长的录音分段发送")
        return w.readframes(nframes), sr


def _pcm_to_wav_b64(pcm: bytes, sample_rate: int = 22050) -> str:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


class AsrEngine:
    """sherpa-onnx 流式识别器（懒加载，线程安全使用由调用方保证）。"""

    def __init__(self, home: Optional[str] = None):
        self.home = Path(home) if home else _models_root()
        self._recognizer = None
        self._ok = False
        self._error = ""

    def status(self) -> dict:
        mdir = self.home / ASR_MODEL_NAME
        missing = [f for f in REQUIRED_FILES
                   if not (mdir / f).is_file()
                   or (mdir / f).stat().st_size
                   < voice_spec.min_bytes_for(f)]
        has_lib = importlib.util.find_spec("sherpa_onnx") is not None
        return {"model": ASR_MODEL_NAME,
                "dir": str(mdir),
                "files_missing": missing,
                "lib_installed": has_lib,
                "ready": has_lib and not missing,
                "error": self._error}

    def _ensure(self) -> None:
        if self._ok:
            return
        st = self.status()
        # 例如：这两处原为 RuntimeError，经 errors.py 归类后用户看到的是
        # 500「操作失败，请稍后重试」。但 status() 同时返回 ready=false、
        # lib_installed=false 与完整 files_missing —— 系统明明知道缺什么，
        # 却把"你还没装模型"说成"服务器故障"，用户重试一百次也不会成功。
        # 这是可自助修复的前提条件，必须是 400 + 可操作指引。
        if not st["lib_installed"]:
            raise UserError(
                "语音识别依赖未安装：请在设置页的语音面板按提示安装后重试")
        if st["files_missing"]:
            raise UserError(
                "语音识别模型未下载完整，缺少：" +
                "、".join(st["files_missing"][:3]) +
                ("等" if len(st["files_missing"]) > 3 else "") +
                "。请在设置页的语音面板查看模型下载来源")
        import sherpa_onnx
        mdir = self.home / ASR_MODEL_NAME
        enc = str(mdir / "encoder-epoch-99-avg-1.int8.onnx")
        dec = str(mdir / "decoder-epoch-99-avg-1.onnx")
        joi = str(mdir / "joiner-epoch-99-avg-1.int8.onnx")
        tokens = str(mdir / "tokens.txt")
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tokens, encoder=enc, decoder=dec, joiner=joi,
            num_threads=2, sample_rate=16000,
            decoding_method="modified_beam_search")
        self._ok = True

    def recognize_wav_b64(self, audio_b64: str) -> dict:
        t0 = time.time()
        try:
            pcm16, sr = _wav_to_pcm16(audio_b64)
        except UserError:
            raise
        except Exception as e:                        # noqa: BLE001
            # 例如：base64 非法 / 非 WAV / 数据被截断，会分别抛出
            # binascii.Error、wave.Error、EOFError、struct.error（全英文），
            # 直冲 do_POST 兜底变成 500「操作失败」——用户只是传错了文件，
            # 却被报成服务器故障，重试一万次也不会成功。
            raise UserError(
                "音频数据无法解析，请重新录制后再试（需为单声道、16 位采样的音频）"
            ) from e
        if sr != 16000:
            # 简单线性重采样到 16k（语音识别可接受）
            ratio = sr / 16000
            n = int(len(pcm16) / 2 / ratio)
            src = struct.unpack(f"<{len(pcm16)//2}h", pcm16)
            pcm16 = struct.pack(f"<{n}h",
                                *(src[int(i * ratio)]
                                  for i in range(n)))
            sr = 16000
        self._ensure()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sr, list(
            struct.unpack(f"<{len(pcm16)//2}h", pcm16)))
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        text = self._recognizer.get_result(stream)
        dur = len(pcm16) / 2 / sr
        return {"text": text.strip(), "duration_s": round(dur, 2),
                "latency_ms": int((time.time() - t0) * 1000)}


def engine(home: Optional[str] = None) -> AsrEngine:
    return AsrEngine(home)
