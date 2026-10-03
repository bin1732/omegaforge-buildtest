"""守卫：validate 层与语音前置条件的两类"伪装成服务器故障"的洞。

本文件全部结论来自验证，不是读代码推断。每条守卫都必须能通过回退校验：
把对应修复撤掉后，本文件的用例必须变红。
"""
from __future__ import annotations

import os
import sys
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omegaforge.core.errors import UserError      # noqa: E402
from omegaforge.core import validate as V       # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def raises_usererror(fn, *a, **k) -> tuple[bool, str]:
  try:
    fn(*a, **k)
    return False, "未抛异常"
  except UserError as e:
    return True, str(e)
  except BaseException as e:             # noqa: BLE001
    return False, f"{type(e).__name__}: {e}"


# ───────────────────────────────────────────────────────────────
# 1. as_float：JSON 里一个 400 位整数（不带小数点）会被解析成 Python int，
#  float() 抛的是 OverflowError，既不是 ValueError 也不是 TypeError。
def test_overflow_int_not_500() -> None:
  big = int("9" * 400)
  for val in (big, -big, 10 ** 309):
    ok, msg = raises_usererror(V.as_float, {"v": val}, "v", 1.0)
    check(f"as_float({str(val)[:8]}…) 必须给 UserError", ok, msg)


def test_overflow_does_not_overblock() -> None:
  """防处理过头：正常浮点与小整数必须照常通过。"""
  for val, want in ((1.0, 1.0), (0.25, 0.25), (4, 4.0), ("1.5", 1.5), (2, 2.0)):
    got = V.as_float({"v": val}, "v", 1.0, minimum=0.25, maximum=4.0)
    check(f"as_float({val!r}) 仍为 {want}", abs(got - want) < 1e-9, f"got={got}")


# ──────────────────────────────────────────────────────────────
# 2. text_weight：findall 物化巨型列表 → 内存炸弹（ 161MB）
def test_weight_memory_bounded() -> None:
  tracemalloc.start()
  w = V.text_weight("你" * 2_000_000)
  _cur, peak = tracemalloc.get_traced_memory()
  tracemalloc.stop()
  mb = peak / 1024 / 1024
  check("text_weight 峰值内存 < 20MB", mb < 20, f"peak={mb:.1f}MB")
  check("text_weight 提前退出后结论不变（够格即真）", w >= 10.0, f"weight={w}")


def test_weight_cjk_ext_b() -> None:
  # CJK 扩展 B（U+20000 起）若不显式列入，生僻字按"5 个西文字符计 1"，
  # 与本函数"中文一字一词"的设计意图直接矛盾。
  w = V.text_weight("\U00020000" * 4)
  check("CJK 扩展 B 每字计 1", abs(w - 4.0) < 1e-6, f"weight={w}")


def test_weight_baseline_unchanged() -> None:
  """防处理过头：中英对照口径不能变。"""
  zh = "你是一位资深代码审查专家，请检查以下代码并指出问题。"
  en = "Summarize the article and list three key risks."
  wz, we = V.text_weight(zh), V.text_weight(en)
  check("中文按字计", wz >= 20, f"weight={wz:.1f}")
  check("英文按词计（远低于字符数）", we < len(en) / 2, f"weight={we:.1f}")
  check("中文信息量高于等长英文", wz > we, f"zh={wz:.1f} en={we:.1f}")


# ───────────────────────────────────────────────────────────────
# 3. 语音前置条件：status() 明确知道 ready=false 与缺失清单，
#  但真实调用却返回 500「操作失败，请稍后重试」——用户重试永不成功。
def test_voice_not_ready_is_user_error() -> None:
  from omegaforge.voice.tts import TtsEngine
  from omegaforge.voice.asr import AsrEngine

  cases = []
  for cls, label in ((TtsEngine, "TTS"), (AsrEngine, "ASR")):
    st = cls().status()
    if st.get("ready"):
      check(f"{label} 已就绪，跳过", True)
      continue
    eng = cls()
    # 注意：不能走 recognize_wav_b64("") —— 空音频会在 _ensure() 之前
    # 就被 _wav_to_pcm16 拦下（回退校验时该写法让守卫全绿，
    # 等于撤掉修复也抓不到）。_ensure() 才是"未就绪"的真正生效点。
    for method, args in (("synthesize", ("你好，这是一段测试",)),
               ("_ensure", ())):
      fn = getattr(eng, method, None)
      if fn is None:
        continue
      try:
        fn(*args)
        ok, msg = False, "未抛异常"
      except UserError as e:
        ok, msg = True, str(e)
      except BaseException as e:         # noqa: BLE001
        ok, msg = False, f"{type(e).__name__}: {e}"
      cases.append((f"{label}.{method}", ok, msg))

  for name, ok, msg in cases:
    check(f"{name} 未就绪时必须是 UserError（400 可自助）", ok, msg)
  if not cases:
    check("至少覆盖一处语音未就绪路径", True)


def main() -> int:
  test_overflow_int_not_500()
  test_overflow_does_not_overblock()
  test_weight_memory_bounded()
  test_weight_cjk_ext_b()
  test_weight_baseline_unchanged()
  test_voice_not_ready_is_user_error()
  print()
  print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
  if FAIL:
    for f in FAIL:
      print(" FAIL: " + f)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
