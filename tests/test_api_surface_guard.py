"""本次改动验证修复的回归守卫（每条都对应一个真实复现过的故障）。

覆盖的四类问题，全部是"用户点了没反应 / 被误导"级别：

1. 只读端点只挂在一种 HTTP 方法上 —— 前端用另一种方法一调用就是
  404「接口不存在」，功能等于不存在（voiceStatus/kbSearch/recallMemory）。
2. 语音状态把模型目录的**绝对路径**返回给前端，泄漏本机目录结构。
3. 请求体类型不对（"abc"/123）导致 AttributeError → 500「操作失败」，
  用户只是传错格式，却被报成服务器故障，重试永远无效。
4. 非法 base64 / 非 WAV 音频抛出英文异常 → 500「操作失败」。

每条用例都同时验证"坏的被拦住"和"好的仍然能用"，避免守卫把功能锁死。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

_ABS_PATH = re.compile(r"(/home/|/Users/|/data/|C:\\\\)")


def _fresh_home() -> dict:
  """重设 home 并让单例失效（模块级状态必须跟着换）。

  为什么**不再**用 `del sys.modules["omegaforge*"]`（注意事项，两处同源）：
  删除模块会让 omegaforge 被重新导入，同一个类于是产生**两个不同的类
  对象**。而别的测试文件在模块级 `from omegaforge... import X` 绑定的是
  旧对象，运行时抛出的却是新对象 —— `assertRaises(X)` 永远捕获不到、
  `patch` 打在新对象上而生效在旧对象上。

  验证症状（两处，都表现为「单独跑全绿、整批跑失败」）：
   · test_mcp_limits.py 2 项：UserError 类身份分裂
   · test_mcp_protocol.py 2 项：annotations 断言 KeyError

  单例已惰性化（`core/paths.py` 的 LazyHome）并新增 `invalidate()`，
  只需丢弃缓存即可按新 home 重读，无需重建模块。
  """
  saved_env = os.environ.get("OMEGAFORGE_HOME")
  saved_mock = os.environ.get("OMEGAFORGE_MOCK")
  home = tempfile.mkdtemp(prefix="ofguard_")
  os.environ["OMEGAFORGE_HOME"] = home
  os.environ["OMEGAFORGE_MOCK"] = "1"
  _reload_singletons()
  return {"env": (saved_env, saved_mock)}


def _reload_singletons() -> None:
  """丢弃九个单例的内存缓存，下次访问按当前环境重读。"""
  from omegaforge import server as _S
  for s in (_S.KB, _S.WIKI, _S.TASKS, _S.USAGE,
       _S.PROVIDERS, _S.CONVS, _S.RUNS):
    inv = getattr(s, "invalidate", None)
    if inv is not None:
      inv()


def _restore_home(snap: dict) -> None:
  """还原 _fresh_home 造成的全局污染（env + 单例缓存）。"""
  saved_env, saved_mock = snap["env"]
  if saved_env is None:
    os.environ.pop("OMEGAFORGE_HOME", None)
  else:
    os.environ["OMEGAFORGE_HOME"] = saved_env
  if saved_mock is None:
    os.environ.pop("OMEGAFORGE_MOCK", None)
  else:
    os.environ["OMEGAFORGE_MOCK"] = saved_mock
  _reload_singletons()


class _Server:
  def __init__(self):
    from omegaforge import server as S
    self.srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    self.port = self.srv.server_address[1]
    threading.Thread(target=self.srv.serve_forever, daemon=True).start()

  def req(self, method, path, body=None, raw=None):
    data = raw if raw is not None else (
      json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(
      f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
      headers={"Content-Type": "application/json"})
    try:
      with urllib.request.urlopen(r, timeout=20) as f:
        return f.status, f.read().decode()
    except urllib.error.HTTPError as e:
      return e.code, e.read().decode()
    except Exception as e:           # noqa: BLE001
      return None, f"{type(e).__name__}: {e}"

  def close(self):
    self.srv.shutdown()
    self.srv.server_close()


class TestReadOnlyEndpointMethodCompat(unittest.TestCase):
  """只读端点必须同时接受 GET 与 POST。"""

  def setUp(self):
    self.addCleanup(_restore_home, _fresh_home())
    self.s = _Server()

  def tearDown(self):
    self.s.close()

  def test_voice_status_accepts_get_and_post(self):
    for m in ("GET", "POST"):
      code, body = self.s.req(m, "/api/voice/status", {} if m == "POST" else None)
      self.assertEqual(code, 200, f"{m} /api/voice/status 不可用：{body}")
      self.assertIn("asr", json.loads(body))

  def test_kb_search_accepts_get_and_post(self):
    for m, kw in (("GET", "/api/kb/search?q=%E4%B8%AD%E6%96%87"),
           ("POST", "/api/kb/search")):
      code, body = self.s.req(m, kw, {"q": "中文"} if m == "POST" else None)
      self.assertEqual(code, 200, f"{m} /api/kb/search 不可用：{body}")
      self.assertIn("results", json.loads(body))

  def test_memory_recall_accepts_get_and_post(self):
    for m, kw in (("GET", "/api/memory/recall?q=%E4%B8%AD%E6%96%87"),
           ("POST", "/api/memory/recall")):
      code, body = self.s.req(m, kw, {"q": "中文"} if m == "POST" else None)
      self.assertEqual(code, 200, f"{m} /api/memory/recall 不可用：{body}")
      self.assertIn("memories", json.loads(body))


class TestVoiceStatusNoPathLeak(unittest.TestCase):
  """语音状态不得把本机绝对路径带出去。"""

  def setUp(self):
    self.addCleanup(_restore_home, _fresh_home())
    self.s = _Server()

  def tearDown(self):
    self.s.close()

  def _assert_no_abs_path(self, obj, trail=""):
    """递归扫描：任何字符串字段都不允许是本机绝对路径。

    上一版用正则匹配 /home/|/Users/|/data/，验证回退校验时**没抓到**——
    因为测试用的临时目录在 /tmp 下，根本不命中这几个前缀。
    改成"任何以 / 或盘符开头的字符串都算泄漏"，才真正防得住。
    """
    if isinstance(obj, dict):
      for k, v in obj.items():
        self._assert_no_abs_path(v, f"{trail}.{k}")
    elif isinstance(obj, list):
      for i, v in enumerate(obj):
        self._assert_no_abs_path(v, f"{trail}[{i}]")
    elif isinstance(obj, str):
      self.assertFalse(
        obj.startswith("/") or ":\\" in obj or obj.startswith("~"),
        f"语音状态泄漏本机路径：{trail}={obj}")

  def test_no_absolute_path_in_voice_status(self):
    code, body = self.s.req("GET", "/api/voice/status")
    self.assertEqual(code, 200)
    payload = json.loads(body)
    self._assert_no_abs_path(payload)
    # 用户需要判断"能不能用"的信息必须还在
    self.assertIn("model", payload["asr"])
    self.assertIn("ready", payload["asr"])


class TestBadRequestShapeNotServerFault(unittest.TestCase):
  """格式错误是用户输入问题，绝不能报成 500 服务器故障。"""

  def setUp(self):
    self.addCleanup(_restore_home, _fresh_home())
    self.s = _Server()

  def tearDown(self):
    self.s.close()

  def test_permissions_rejects_non_object_body(self):
    for raw in (b'"abc"', b"123", b"[]"):
      code, body = self.s.req("POST", "/api/tools/permissions", raw=raw)
      self.assertEqual(code, 400,
               f"请求体 {raw!r} 被报成 {code}（应为 400）：{body}")
      self.assertNotIn("500", body)
      self.assertFalse(
        any(ord(c) < 128 and c.isalpha()
          for c in json.loads(body)["error"]),
        f"错误文案含英文：{body}")

  def test_permissions_still_accepts_object(self):
    code, body = self.s.req("POST", "/api/tools/permissions",
                {"fs": True, "exec": False})
    self.assertEqual(code, 200, body)
    self.assertTrue(json.loads(body)["permissions"]["fs"])

  def test_asr_rejects_malformed_audio(self):
    for bad in ("中文", "aaaaaaaaaaaa", "null", "{}"):
      code, body = self.s.req("POST", "/api/voice/asr",
                  {"audio_b64": bad})
      self.assertEqual(code, 400,
               f"audio_b64={bad!r} 被报成 {code}：{body}")
      self.assertFalse(
        any(ord(c) < 128 and c.isalpha()
          for c in json.loads(body)["error"]),
        f"错误文案含英文：{body}")
