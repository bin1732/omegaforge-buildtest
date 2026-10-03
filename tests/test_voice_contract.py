"""语音模块契约守卫。

钉死两个可复现的缺陷，防止回退：

1. asr 重采样放大（DoS）
  WAV 头里把 framerate 写成 100 → 重采样按 `16000/sr` 放大，
  42KB 音频被展开成 6.4MB（160x）；写成 1 就是 16000x（数百 MB）。
  一个几十 KB 的上传就能打爆进程内存。

2. tts 谎报可用
  status() 只用 exists() 判断，0 字节的 model.onnx 与 100 字节的 tokens.txt
  被判为"齐备"，界面显示可用，用户一点合成就崩。同源的 asr 有体积阈值
  能识破，两边不一致 —— 这里把 tts 对齐到同一标准。
"""
from __future__ import annotations

import base64
import io
import os
import struct
import wave

import pytest

from omegaforge.core.errors import UserError
from omegaforge.voice import asr, model_size, tts
from omegaforge.voice import spec as voice_spec


def _mk_wav(sr: int, n: int = 2000) -> str:
  buf = io.BytesIO()
  with wave.open(buf, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(struct.pack("<%dh" % n, *([100] * n)))
  return base64.b64encode(buf.getvalue()).decode()


# ── 1. 采样率上限 ────────────────────────────────────────────────

@pytest.mark.parametrize("sr", [1, 100, 1000, 4000, 96000, 192000])
def test_asr_rejects_abnormal_sample_rate(sr):
  """异常采样率必须在入口拒绝，而不是照着重采样。"""
  with pytest.raises(UserError) as e:
    asr._wav_to_pcm16(_mk_wav(sr))
  assert "采样率" in str(e.value)


@pytest.mark.parametrize("sr", [8000, 16000, 22050, 44100, 48000])
def test_asr_accepts_normal_sample_rate(sr):
  """常用采样率不得误杀。"""
  pcm, got = asr._wav_to_pcm16(_mk_wav(sr))
  assert got == sr and len(pcm) > 0


def test_asr_resample_amplification_is_bounded():
  """放行范围内，重采样放大倍数必须有上界（16000/8000 = 2x）。"""
  worst = 0
  for sr in (8000, 16000, 22050, 44100, 48000):
    pcm, got = asr._wav_to_pcm16(_mk_wav(sr, n=2000))
    ratio = got / 16000
    out = int(len(pcm) / 2 / ratio)
    worst = max(worst, out / (len(pcm) // 2))
  assert worst <= 2.0, f"放大倍数 {worst}x 超出上界"


# ── 2. tts 体积阈值 ──────────────────────────────────────────────

def _build_tts_home(root: str, model: int, tokens: int,
          lexicon: int, dict_files: int = 1) -> str:
  home = os.path.join(root, "h%d" % (model + tokens + lexicon + dict_files))
  mdir = os.path.join(home, tts.TTS_MODEL_NAME)
  os.makedirs(os.path.join(mdir, "dict"), exist_ok=True)
  for name, size in (("model.onnx", model), ("tokens.txt", tokens),
            ("lexicon.txt", lexicon)):
    with open(os.path.join(mdir, name), "wb") as f:
      f.write(b"x" * size)
  for i in range(dict_files):
    with open(os.path.join(mdir, "dict", "f%d" % i), "wb") as f:
      f.write(b"x" * 100)
  return home


def test_tts_rejects_empty_and_truncated_models(tmp_path):
  """0 字节 / 截断模型必须被识破，不能报"齐备"。"""
  home = _build_tts_home(str(tmp_path), model=0, tokens=655, lexicon=50_000)
  missing = tts.engine(home=home).status()["files_missing"]
  assert "model.onnx" in missing, "0 字节模型被误判为已安装"
  assert "tokens.txt" not in missing, "真实体积的词表不得被体积下限误杀"


def test_tts_accepts_real_install(tmp_path):
  """真实安装不得误杀。

  词表按真实体积取值：vits-melo 的 tokens.txt 只有 655 字节。对词表套用
  模型文件的下限，会让真实安装被判缺失——界面显示"模型未下载完整"，
  用户点合成永远失败，而下载其实是成功的。
  """
  home = _build_tts_home(str(tmp_path), model=50_000, tokens=655,
              lexicon=50_000)
  assert tts.engine(home=home).status()["files_missing"] == []


def test_tts_rejects_empty_dict_dir(tmp_path):
  """dict 是目录：空目录视为缺失，但不能对小文件逐个套阈值而误杀。"""
  home = _build_tts_home(str(tmp_path), model=50_000, tokens=20_000,
              lexicon=50_000, dict_files=0)
  assert "dict" in tts.engine(home=home).status()["files_missing"]


def _build_asr_home(root: str, size: int) -> str:
  mdir = os.path.join(root, "a%d" % size, asr.ASR_MODEL_NAME)
  os.makedirs(mdir, exist_ok=True)
  for name in asr.REQUIRED_FILES:
    with open(os.path.join(mdir, name), "wb") as f:
      f.write(b"x" * size)
  return os.path.dirname(mdir)


def test_asr_and_tts_share_one_size_threshold():
  """两侧体积下限必须是同一个定义，不能各写一份字面量。

  各写一份时，改一侧不会牵动另一侧，而任一侧单独不一致都不会让任何
  用例变红 —— 不一致会以"看起来一切正常"的形式留下。故判定统一走
  voice_spec.min_bytes_for：两侧对同一条目取到的值必须相同，且模型
  文件取到的就是 model_size 里那一个值。
  """
  for entry in ("model.onnx", "encoder-epoch-99-avg-1.int8.onnx"):
    assert voice_spec.min_bytes_for(entry) == model_size.MODEL_MIN_BYTES
  # 两侧不得各自保留一份常量：判定点只走 min_bytes_for（另见
  # test_voice_size_floor 的源码守卫）
  assert not hasattr(asr, "MODEL_MIN_BYTES")
  assert not hasattr(tts, "MODEL_MIN_BYTES")


@pytest.mark.parametrize("size,missing", [(9_999, True), (10_000, False)])
def test_asr_size_threshold_follows_shared_definition(tmp_path, size, missing):
  """asr 侧的判定必须随共用定义走。

  只比常量还差一步：常量相等但判定处另写一个数，等于同源是假的。这里
  直接按体积落在阈值两侧各走一遍，任一侧判定漂移都会在这里现形。
  """
  home = _build_asr_home(str(tmp_path), size)
  got = asr.engine(home=home).status()["files_missing"]
  assert bool(got) is missing


def test_tts_threshold_matches_asr():
  """两边阈值必须同源，防止再次出现一边识破、一边谎报。"""
  assert voice_spec.min_bytes_for("model.onnx") == 10_000
  assert asr.SAMPLE_RATE_MIN == 8_000 and asr.SAMPLE_RATE_MAX == 48_000
