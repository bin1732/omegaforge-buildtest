# -*- coding: utf-8 -*-
"""voice 层边界守卫（续）——覆盖度表里两个 0 命中盲区。

`voice/asr.py`（171 行）与 `voice/tts.py`（122 行）缺少该约束时未被章节点名。

## asr：字节上限拦不住时长（核心）

HTTP 侧有 `MAX_BODY_BYTES = 8MB`，但那是**字节数**。同一个 8MB 请求：

  48kHz -> 62 秒   8kHz -> 375 秒

而内存放大是**按样本数**的常数倍（`struct.unpack` 把每个样本物化成一个
Python int，验证 6.7x）：

  10s @48kHz PCM 1.0MB  峰值  6.4MB
  60s @48kHz PCM 5.8MB  峰值 39.2MB
  300s @48kHz PCM 28.8MB 峰值 192.7MB

所以约束必须落在样本数（= 时长）上，字节上限拦不住。
判定用 WAV 头的 nframes（O(1)，在 readframes 之前），两个方向都是安全侧：
 · 头声明小于实际数据 -> readframes 只按声明量读，不会多读；
 · 头声明大于实际数据 -> 这里先拒，宁可误拒也不多读。

## tts：一处潜伏的英文 ValueError

`raise ValueError("text required")` 在 HTTP 上**不可达**（`need("text")` 先拦），
属潜伏而非活跃。但仍要修：ValueError 经 errors.py 归类后落到通用文案，
那句英文到不了用户眼前；一旦出现非 HTTP 调用方就会变成"不知道缺什么"。
"""

from __future__ import annotations

import base64
import io
import os
import sys
import unittest
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core.errors import UserError       # noqa: E402
from omegaforge.voice import asr, tts          # noqa: E402


def _wav_b64(seconds: int, sr: int, ch: int = 1, width: int = 2) -> str:
  buf = io.BytesIO()
  with wave.open(buf, "wb") as w:
    w.setnchannels(ch)
    w.setsampwidth(width)
    w.setframerate(sr)
    w.writeframes(b"\x00\x01" * (sr * seconds))
  return base64.b64encode(buf.getvalue()).decode()


# ───────────────────────────────────────────────────────────────
# 1. asr 上限（单位是样本数，不是秒——见 asr.MAX_PCM_SAMPLES 的）
class AsrDurationCapTest(unittest.TestCase):

  def test_over_budget_rejected(self):
    """样本数超出预算一律拒绝（用例都 < 15MB，跑得动）。"""
    for secs, sr in ((301, 8000),   # 2.41M 样本
             (200, 16000),   # 3.20M
             (100, 48000)):  # 4.80M
      with self.assertRaises(
          UserError, msg=f"{secs}s@{sr}Hz 超样本预算，应拒绝"):
        asr._wav_to_pcm16(_wav_b64(secs, sr))

  def test_old_seconds_cap_case_is_now_rejected(self):
    """回归校验点：上一次改动的 300 秒上限在 48kHz 下会放行 1440 万样本。

    按验证 17.9x 放大，那等于约 493MB 峰值——上限形同虚设。
    这条钉住「单位必须是样本数」：同样的音频在 8kHz 下（240 万样本）
    恰好不超预算，在 48kHz 下必须被拒。
    """
    # 300s @8kHz = 2.40M 样本 = 恰好等于预算，放行（判据是 > 而非 >=）
    pcm, _ = asr._wav_to_pcm16(_wav_b64(300, 8000))
    self.assertEqual(len(pcm) // 2, 2_400_000)
    # 同样的 300 秒在 48kHz 下是 1440 万样本，必须拒
    with self.assertRaises(UserError, msg="300s@48kHz 样本数超预算，应拒绝"):
      asr._wav_to_pcm16(_wav_b64(300, 48000))

  def test_rejection_peak_is_bounded(self):
    """拒绝路径的峰值必须有界——这是「判定在物化之前」的唯一可观测证据。

    注意：readframes 返回的是紧凑 bytes，本身不放大（验证读 310s@8kHz
    只占 11MB）。真正的 17.9x 放大在 recognize_wav_b64 的 struct.unpack。
    所以这条验的不是"有没有提前 readframes"，而是"有没有走到物化"。

    同时做**对照测量**：把同一份 PCM 真的 unpack 一次，峰值必须显著
    大于拒绝路径。没有对照的话，"峰值 < 40MB" 这种断言可能因为
    阈值本来就远大于实际峰值而恒真（假守卫）。
    """
    import struct
    import tracemalloc

    over = _wav_b64(100, 48000)     # 4.8M 样本
    tracemalloc.start()
    try:
      asr._wav_to_pcm16(over)
      self.fail("100s@48kHz 应被拒绝")
    except UserError:
      pass
    reject_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # 对照：同样大小的 PCM 真的物化一次
    pcm_bytes = b"\x01\x02" * 2_400_000
    tracemalloc.start()
    src = struct.unpack(f"<{2_400_000}h", pcm_bytes)
    control_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    del src

    rm = reject_peak / 1048576
    cm = control_peak / 1048576
    self.assertLess(rm, 40,
            f"拒绝路径峰值 {rm:.1f}MB —— 判定发生在物化之后")
    self.assertGreater(cm, 2 * rm + 20,
              f"对照组峰值只有 {cm:.1f}MB，与拒绝路径 {rm:.1f}MB "
              f"拉不开差距 —— 断言阈值形同虚设，等于没验")

  def test_budget_is_the_bound(self):
    """放行的音频，样本数不得超过预算（否则峰值无从约束）。"""
    for secs, sr in ((1, 16000), (30, 16000), (150, 16000), (50, 48000)):
      pcm, got = asr._wav_to_pcm16(_wav_b64(secs, sr))
      self.assertEqual(got, sr, f"{secs}s@{sr} 采样率回传错误")
      self.assertLessEqual(len(pcm) // 2, asr.MAX_PCM_SAMPLES,
                 f"{secs}s@{sr} 样本数超出预算")
      self.assertEqual(len(pcm) // 2, secs * sr,
               f"{secs}s@{sr} 样本数不对: {len(pcm)//2}")

  def test_message_names_seconds_and_limit(self):
    """用户看的是秒数：文案必须给出该采样率下的实际上限。"""
    try:
      asr._wav_to_pcm16(_wav_b64(100, 48000))
      self.fail("应拒绝")
    except UserError as e:
      msg = str(e)
      self.assertIn("过长", msg)
      self.assertIn("48kHz", msg, f"应点名当前采样率下的上限: {msg}")
      self.assertIn("50", msg, f"48kHz 下上限约 50 秒: {msg}")

  def test_format_checks_not_superseded(self):
    """防处理过头：采样率 / 声道校验不得被时长判定顶替。"""
    for sr, ch, width, want in (
        (100, 1, 2, "采样率异常"),
        (100000, 1, 2, "采样率异常"),
        (16000, 2, 2, "音频格式有误"),
        (16000, 1, 1, "音频格式有误"),
    ):
      with self.assertRaises(UserError, msg=f"sr={sr} ch={ch} 应拒绝"):
        asr._wav_to_pcm16(_wav_b64(1, sr, ch=ch, width=width))
      # 文案必须仍然点名具体原因，不是通用的"音频过长"
      try:
        asr._wav_to_pcm16(_wav_b64(1, sr, ch=ch, width=width))
      except UserError as e:
        self.assertIn(want, str(e), f"文案应含「{want}」: {e}")

  def test_broken_audio_still_user_error(self):
    """防处理过头：非法 base64 / 非 WAV 仍要给中文 UserError，不是 500。

    必须走 `recognize_wav_b64` 而不是 `_wav_to_pcm16`——把二进制异常
    转成中文 UserError 的那层 `except Exception` 在前者里，后者会把
    binascii.Error / wave.Error / EOFError / struct.error 原样抛出来。
    （端到端验证：POST /api/voice/asr 传 "!!!" -> 400「音频数据无法解析」）
    """
    for bad in ("!!!", "AAAA", base64.b64encode(b"not a wav").decode()):
      with self.assertRaises(UserError, msg=f"{bad[:12]} 应给 UserError"):
        asr.engine().recognize_wav_b64(bad)


# ───────────────────────────────────────────────────────────────
# 2. tts 空文本：中文 UserError，不是英文 ValueError
class TtsEmptyTextTest(unittest.TestCase):
  def test_empty_text_gives_chinese_user_error(self):
    for bad in ("", "  ", "\t\n"):
      with self.assertRaises(UserError, msg=f"{bad!r} 应给 UserError"):
        tts.TtsEngine(home="/tmp/of_tts_none").synthesize(bad)

  def test_message_is_chinese_not_english(self):
    try:
      tts.TtsEngine(home="/tmp/of_tts_none").synthesize("  ")
    except UserError as e:
      self.assertNotIn("text required", str(e),
               "英文内部串不得外泄")
      self.assertIn("文字", str(e), f"应给出中文原因: {e}")

  def test_value_error_not_leaked(self):
    """必须是 UserError（400 语义），不是 ValueError（会被压成通用文案）。"""
    try:
      tts.TtsEngine(home="/tmp/of_tts_none").synthesize("")
      self.fail("空文本应抛异常")
    except UserError:
      pass           # 正确
    except ValueError as e:    # noqa: PERF203
      self.fail(f"空文本抛了 ValueError（会被压成通用文案）: {e}")


if __name__ == "__main__":
  unittest.main()
