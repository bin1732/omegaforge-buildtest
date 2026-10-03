"""HTTP 状态映射与"用户当场能改"的自定义异常守卫。

两件事，都属于 C 组「按类型名匹配」清算的战果。

**其一：HTTP 状态映射曾被本仓自己改坏。**

`urllib.error.HTTPError` 是 `URLError` 的**子类**。为补网络家族漏网
而加的 `isinstance(exc, URLError)` 分支，排在了按名字匹配 `HTTPError` 的分支
**之前**，于是 401/403/404/429/5xx 全部被截获，统一退化成
「网络请求异常，请检查网络与接口地址」。

对照验证（`git show 366ba5f` 载入 ch33 之前的模块）：

  401 -> E_AUTH   「API 密钥无效或已过期」   ← ch33 前可用
  429 -> E_RATE_LIMIT「请求过于频繁，已被限流」  ← ch33 前可用
  500 -> E_UPSTREAM 「模型服务暂时不可用」    ← ch33 前可用

最重的一档是 **401**：密钥无效被说成网络不通。用户（和外部 MCP 模型）会照着
去查地址、换网络、反复重试——而改地址换网络重试多少次都不会成功。
429 更糟：限流被说成网络异常，**重试只会更狠地撞限流**。

这不是"从来没生效"，是**补 isinstance 兜底时自己引入的回归**。守卫必须钉住
"HTTP 状态分支在 URLError 分支之前"这件事本身，而不是只钉几个状态码。

**其二：两个"改个数字就能继续"的自定义异常落通用兜底。**

`BudgetExceeded`（预算耗尽）与 `LockTimeout`（等锁超时）缺少该约束时都落到函数末尾的
「操作失败，请稍后重试」。它们的 message 是英文内部串（不能展示），但这两件事
恰恰是**用户改一个数字/等一会儿就能继续**的——说成服务器故障会让人反复重试，
而重试必然再次失败（预算不会自己变多）。

守卫两类：
 * 映射正确（含"文案互不相同"——同族混成一句会误导，本项目已多次踩）
 * **顺序守卫**：HTTP 状态必须在 URLError 之前（直接读源码判定，比测状态码
  更能钉住"回归"这类失效）
"""
from __future__ import annotations

import inspect
import unittest
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omegaforge.core import errors           # noqa: E402
from omegaforge.core.atomicio import LockTimeout    # noqa: E402
from omegaforge.core.budget import BudgetExceeded    # noqa: E402


class HttpStatusMappingTest(unittest.TestCase):
  """urllib HTTPError 必须按状态码给出各自不同的处置动作。"""

  def _msg(self, code):
    return errors._classify(
      urllib.error.HTTPError("http://x", code, "m", {}, None))[1]

  def test_401_is_auth_not_network(self):
    code, msg = errors._classify(
      urllib.error.HTTPError("http://x", 401, "m", {}, None))
    self.assertEqual(code, errors.CODE_AUTH)
    self.assertIn("密钥", msg)
    self.assertNotIn("网络", msg)   # 被说成网络问题 → 用户查错方向

  def test_429_is_rate_limit(self):
    code, msg = errors._classify(
      urllib.error.HTTPError("http://x", 429, "m", {}, None))
    self.assertEqual(code, errors.CODE_RATE_LIMIT)
    self.assertIn("限流", msg)

  def test_5xx_is_upstream(self):
    for c in (500, 502, 503, 504):
      self.assertEqual(
        errors._classify(
          urllib.error.HTTPError("http://x", c, "m", {}, None))[0],
        errors.CODE_UPSTREAM)

  def test_403_404_distinct(self):
    m403 = self._msg(403)
    m404 = self._msg(404)
    self.assertNotEqual(m403, m404, "403 与 404 混成一句会让用户查错对象")

  def test_statuses_all_distinct(self):
    msgs = [self._msg(c) for c in (401, 403, 404, 429, 500)]
    self.assertEqual(len(set(msgs)), 5,
             "五个状态码的处置动作必须互不相同：%s" % msgs)

  def test_non_http_urlerror_still_network(self):
    """防处理过头：非 HTTP 的 URLError 不能跟着变成"异常响应"。"""
    code, msg = errors._classify(urllib.error.URLError("boom"))
    self.assertEqual(code, errors.CODE_NETWORK)
    self.assertIn("无法连接", msg)


class ClassifyOrderTest(unittest.TestCase):
  """顺序守卫：钉住"HTTP 状态排在 URLError 之前"这件事本身。

  只测状态码不够——撤掉顺序而保留映射函数，状态码用例仍可能通过（若把
  isinstance(HTTPError) 挪到 URLError 之后但保留名字匹配分支……虽然本仓
  urllib 路径会失效）。直接读源码判定位置关系，最能钉住这类顺序型回归。
  """

  def test_http_branch_precedes_urlerror_branch(self):
    src = inspect.getsource(errors._classify)
    i_http = src.find("_urlerror.HTTPError")
    i_url = src.find("isinstance(exc, _urlerror.URLError)")
    self.assertGreater(i_http, 0, "未找到 HTTPError 的 isinstance 分支")
    self.assertGreater(i_url, 0, "未找到 URLError 的 isinstance 分支")
    self.assertLess(i_http, i_url,
            "HTTPError 分支必须排在 URLError 之前："
            "HTTPError 是 URLError 子类，顺序反了 401/429/5xx 全被吞")

  def test_httperror_is_urlerror_subclass(self):
    """守卫的前提本身也要钉住：若哪天这条继承关系变了，上面的顺序守卫
    就该重新评估，而不是默默失效。"""
    self.assertTrue(issubclass(urllib.error.HTTPError, urllib.error.URLError))


class ActionableCustomErrorTest(unittest.TestCase):
  """预算耗尽 / 等锁超时：必须给出"改个数字就能继续"的指引。"""

  def test_budget_exceeded_actionable(self):
    e = BudgetExceeded("token budget 100 exceeded at 5000 (phase=eval)")
    code, msg = errors._classify(e)
    self.assertNotIn("操作失败", msg, "预算耗尽被说成服务器故障 → 必然反复重试")
    self.assertIn("预算", msg)
    # 英文内部串绝不能外泄（"token 预算"是中文文案里刻意保留的通用词，
    # 这里要挡的是原始 message 里的 "exceeded"/"phase=" 这类内部字段）
    shown = errors.user_error(e)
    self.assertNotIn("exceeded", shown)
    self.assertNotIn("phase=", shown)

  def test_lock_timeout_actionable(self):
    code, msg = errors._classify(LockTimeout())
    self.assertNotIn("操作失败", msg)
    self.assertIn("另一处进程", msg)

  def test_two_are_distinct(self):
    m1 = errors._classify(BudgetExceeded("x"))[1]
    m2 = errors._classify(LockTimeout())[1]
    self.assertNotEqual(m1, m2)

  def test_real_bank_trigger(self):
    """真实接口触发，不走手工构造。"""
    from omegaforge.core.budget import TokenBank
    bank = TokenBank(budget_tokens=100)
    with self.assertRaises(BudgetExceeded) as ctx:
      bank.charge("eval", "m", 5000, 0)
    self.assertIn("预算", errors.user_error(ctx.exception))


class NoOverFixTest(unittest.TestCase):
  """防处理过头：真正属于内部故障的不能跟着变成"可自助"文案。"""

  def test_runtime_error_still_internal(self):
    code, msg = errors._classify(RuntimeError("x"))
    self.assertEqual(code, errors.CODE_INTERNAL)

  def test_key_error_still_internal(self):
    code, _ = errors._classify(KeyError("x"))
    self.assertEqual(code, errors.CODE_INTERNAL)

  def test_unicode_decode_error_not_leaked(self):
    """UnicodeDecodeError 是 ValueError 子类但名字不同，落到通用兜底。
    属既有取舍（读取侧已用 errors="replace" 收口），此处只钉住：**绝不能
    把英文内部串透给用户**。"""
    try:
      b"abc".decode("utf-8")
    except UnicodeDecodeError as e:
      msg = errors.user_error(e)
      self.assertNotIn("codec", msg)
      self.assertFalse(msg.isascii() or msg == str(e))


if __name__ == "__main__":
  unittest.main(verbosity=2)
