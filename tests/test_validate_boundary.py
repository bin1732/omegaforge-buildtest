# -*- coding: utf-8 -*-
"""core/validate.py 边界守卫——该模块缺少该约束时未被系统审计。

覆盖度表里 `omegaforge/core/validate.py`（284 行）的文档命中数为 0，
是最大的未审计盲区；而它恰好是产品"可验证"主张的入参守门层。

## 核心失效：as_int 的 str 分支里有一行错位加固

  val = int(s)
  if not math.isfinite(val):    # ← 想防 NaN/Infinity
    raise UserError(...)

验证（真实 HTTP，POST /api/tasks/add 的 priority）：

 | priority 位数 | 缺少该约束时 | 加上该约束后 |
 |---|---|---|
 | 100 / 308 / 309 | 400 不能大于 5 | 400 不能大于 5 |
 | **400 / 4000 / 4300** | **500 操作失败** | **400 不能大于 5** |
 | 4301 / 5000 | 400 不是有效数值 | 400 不是有效数值 |

两处同时成立，才说明它是错位加固而非"少了个检查"：

1. **它防的情况根本不会从这里进来**。`int()` 的结果恒为有限值；
  NaN / Infinity 由 json.loads 解析成 **float**，走的是 float 分支，
  那边已用 `math.isfinite` 正确拦住（`test_nan_infinity_still_blocked`）。
2. **它引入了新的崩溃**。`math.isfinite(超大 int)` 要先转 float，
  超出 float 量程抛 **OverflowError**——既非 ValueError 也非 TypeError，
  原样穿透成 500。

于是出现"越离谱越安全"的荒谬区间：4301 位以上被 Python 的
`sys.get_int_max_str_digits`（验证 4300）拦成 400，而中间那段
（约 309~4300 位）反而崩成 500。

## 另两处（同族："假值被当成有效填写"）

- `require({"tags": []})` 原本放行 —— 空容器等于没填，界面上必填项
 看着是空的却能提交成功。但 0 / False 必须**继续算填写**（原注释验证过：
 数值 0 是合法输入）。
- `pick_first({"source_prompt": false})` 原本返回字符串 `"False"`，
 把"没有源提示词"当成"源提示词是 False"，把兼容兜底链在第一站就截断。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from omegaforge.core.errors import UserError     # noqa: E402
from omegaforge.core import validate as V       # noqa: E402

MAX_DIGITS = sys.get_int_max_str_digits()  # 4300（Python ≥3.10.7）


def _call(fn, *a, **k) -> tuple[str, object]:
  """返回 ("ok", 值) / ("user", 文案) / ("{异常名}", str(e))。"""
  try:
    return "ok", fn(*a, **k)
  except UserError as e:
    return "user", str(e)
  except BaseException as e:             # noqa: BLE001
    return type(e).__name__, str(e)


# ───────────────────────────────────────────────────────────────
# 1. as_int：字符串分支不得因位数产生非 UserError 异常（核心）
class AsIntDigitWidthTest(unittest.TestCase):
  def test_wide_digit_string_never_raises_non_usererror(self):
    for n in (100, 308, 309, 310, 400, 1000, 4000, MAX_DIGITS, MAX_DIGITS + 1):
      kind, out = _call(V.as_int, {"v": "1" * n}, "v", 1, maximum=5)
      self.assertEqual(
        kind, "user",
        f'{n} 位数字字符串应给 UserError(400)，实际 {kind}: '
        f'{str(out)[:60]}')

  def test_wide_digit_string_message_names_the_field(self):
    """报错必须落在字段上，不是通用「操作失败」。"""
    kind, out = _call(V.as_int, {"v": "1" * 400}, "v", 1, maximum=5)
    self.assertEqual(kind, "user")
    self.assertIn("不能大于 5", str(out), f"实际文案: {out}")

  def test_plain_int_input_not_affected(self):
    """裸 int（非字符串）分支本来就没有这行，确认它一直正常。"""
    for val, want in ((3, 3), (10**100, 10**100), (0, 0), (-5, -5)):
      got = V.as_int({"v": val}, "v", 1)
      self.assertEqual(got, want, f"as_int({val}) got={got}")


class AsIntNonFiniteStillBlockedTest(unittest.TestCase):
  """防处理过头：删掉那行之后，NaN / Infinity 必须仍被拦住。

  它们走的是 **float 分支**（json.loads 把 NaN/Infinity 解析成 float），
  与被测的那行无关——这条守卫正是"那行是死代码"的实证。
  """

  def test_nan_infinity_still_blocked(self):
    for lit in ("NaN", "Infinity", "-Infinity"):
      payload = json.loads('{"v": %s}' % lit)
      kind, out = _call(V.as_int, payload, "v", 1, maximum=5)
      self.assertEqual(kind, "user", f"{lit} 应给 UserError，实际 {kind}")
      self.assertIn("有效数字", str(out), f"{lit} 文案: {out}")

  def test_bad_string_still_blocked(self):
    for s in ("abc", "1.5", "0x10", ""):
      kind, _ = _call(V.as_int, {"v": s}, "v", 1, maximum=5)
      # 空串走 default 分支，合法
      if s == "":
        self.assertEqual(kind, "ok", "空串应回落 default")
      else:
        self.assertEqual(kind, "user", f"{s!r} 应给 UserError")


# ───────────────────────────────────────────────────────────────
# 2. 端到端：真实 HTTP 上不得出现 500（唯一能证明"不是 500"的方式）
class EndToEndPriorityWidthTest(unittest.TestCase):
  """起真服务发真请求——单元层断言"抛的是 UserError"，
  而"UserError 是否真的变成 400 而非 500"只有 HTTP 层说得清。"""

  @classmethod
  def setUpClass(cls):
    cls._snap = os.environ.get("OMEGAFORGE_HOME")
    cls._home = tempfile.mkdtemp(prefix="of_vb_")
    os.environ["OMEGAFORGE_HOME"] = cls._home
    import omegaforge.server as S
    cls._srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    cls._port = cls._srv.server_address[1]
    threading.Thread(target=cls._srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

  @classmethod
  def tearDownClass(cls):
    cls._srv.shutdown()
    if cls._snap is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = cls._snap

  def _post(self, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
      f"http://127.0.0.1:{self._port}/api/tasks/add",
      data=json.dumps(payload).encode(),
      headers={"Content-Type": "application/json"}, method="POST")
    try:
      with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
      return e.code, e.read().decode()

  def test_wide_priority_is_400_not_500(self):
    for n in (400, 4000, MAX_DIGITS):
      code, body = self._post({"text": "任务", "priority": "1" * n})
      self.assertNotEqual(
        code, 500,
        f"{n} 位 priority 返回 500「操作失败」——用户只是填了个大数字，"
        f"却被报成服务器故障。body={body[:120]}")
      self.assertEqual(code, 400, f"{n} 位 期望 400，实际 {code}")

  def test_normal_priority_still_works(self):
    """防处理过头：合法 priority 必须照常 200。"""
    code, body = self._post({"text": "任务", "priority": 3})
    self.assertEqual(code, 200, f"合法 priority=3 应 200，实际 {code}: {body[:120]}")


# ───────────────────────────────────────────────────────────────
# 3. require：空容器 = 没填；0 / False = 已填
class RequireEmptyContainerTest(unittest.TestCase):
  def test_empty_container_counts_as_missing(self):
    for v in ([], (), {}, set()):
      with self.assertRaises(UserError, msg=f"require({v!r}) 应报缺失"):
        V.require({"a": v}, ["a"])

  def test_non_empty_container_counts_as_filled(self):
    V.require({"a": ["x"]}, ["a"])
    V.require({"a": {"k": 1}}, ["a"])

  def test_zero_and_false_still_count_as_filled(self):
    """防处理过头：数值 0 与布尔 False 是合法填写，不得被判缺失。"""
    V.require({"a": 0}, ["a"])
    V.require({"a": False}, ["a"])

  def test_whitespace_still_missing(self):
    with self.assertRaises(UserError):
      V.require({"a": "  "}, ["a"])


# ───────────────────────────────────────────────────────────────
# 4. pick_first：bool / 容器不算文本，数值仍算
class PickFirstFalseValueTest(unittest.TestCase):
  def test_bool_is_not_text(self):
    self.assertEqual(V.pick_first({"a": False}, ["a"], "DFLT"), "DFLT")
    self.assertEqual(V.pick_first({"a": True}, ["a"], "DFLT"), "DFLT")

  def test_container_is_not_text(self):
    self.assertEqual(V.pick_first({"a": {"x": 1}}, ["a"], "DFLT"), "DFLT")
    self.assertEqual(V.pick_first({"a": [1]}, ["a"], "DFLT"), "DFLT")

  def test_skipping_continues_to_next_key(self):
    """跳过的键不得截断兜底链，必须继续找下一个。"""
    self.assertEqual(V.pick_first({"a": False, "b": "yes"}, ["a", "b"]), "yes")

  def test_number_still_usable(self):
    """防处理过头：id 常以数字传来，str(123)=='123' 必须保留。"""
    self.assertEqual(V.pick_first({"id": 123}, ["id"]), "123")
    self.assertEqual(V.pick_first({"a": 0}, ["a"]), "0")

  def test_normal_text_unchanged(self):
    self.assertEqual(V.pick_first({"a": " hello "}, ["a"]), "hello")
    self.assertEqual(V.pick_first({}, ["a"], "DFLT"), "DFLT")


if __name__ == "__main__":
  unittest.main()
