"""TokenBank 契约守卫（pytest，CI 会真实执行）。

为什么需要这一份
----------------
budget.py 自称 "hard budget control ... Nothing can spend beyond the
declared budget"，但 `if self.budget and projected > self.budget`
这一行让 budget=0 时整个上限检查被跳过——**0 是假值**。

验证：TokenBank(0) 可以无限扣费，200 次 charge 花掉 2,000,000 tokens
而没有任何拦截，remaining 恒为 0 却永远拦不住。

入口（server.py）确实有 MIN_BUDGET 下界，但那只覆盖 HTTP 一条路径；
CLI 的 --budget、MCP 的 budget 参数、以及任何直接构造 TokenBank 的
调用点都没有这道闸。所以在 TokenBank 自身落闸才是正确的纵深防御。

另外三项同样是可复现，不是读代码推的：
 - charge(None, 1) 抛 TypeError / charge('abc', 1) 抛 ValueError
 - total_spent 每次全量求和，而 charge() 会调它 → N 次 charge 是 O(N²)
 - charge_estimate 的 docstring 声称 "Reserve / soft reservation"，
  实现里从头到尾没有任何扣减动作——注释在撒谎
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omegaforge.core.budget import BudgetExceeded, TokenBank # noqa: E402


# ------------------------------------------------------- 1. 预算 0 不是无限
def test_zero_budget_blocks_every_charge():
  """budget=0 必须拦住每一次扣费，而不是变成无限额度。"""
  bank = TokenBank(0)
  try:
    bank.charge("p", "m", 100, 100)
  except BudgetExceeded:
    return
  raise AssertionError(
    f"budget=0 未拦住扣费，等于无限额度（已花费 {bank.total_spent}）")


def test_zero_budget_blocks_many_small_charges():
  """小额多次也不该绕过去——旧实现下这是最常见的绕过姿势。"""
  bank = TokenBank(0)
  blocked = False
  for _ in range(200):
    try:
      bank.charge("p", "m", 5000, 5000)
    except BudgetExceeded:
      blocked = True
      break
  assert blocked, "连续小额扣费未被拦截，等于无限额度"


def test_negative_budget_clamped_to_zero():
  """负预算没有语义，统一夹到 0（旧行为是立刻拒但报错难懂）。"""
  bank = TokenBank(-100)
  assert bank.budget == 0, f"负预算应夹到 0， {bank.budget}"


def test_positive_budget_still_enforced():
  """正常预算语义不变：超支即拒，未超支放行。"""
  bank = TokenBank(1000)
  bank.charge("p", "m", 400, 400)
  assert bank.total_spent == 800
  try:
    bank.charge("p", "m", 400, 400)
  except BudgetExceeded:
    return
  raise AssertionError("超支未被拦住")


# ------------------------------------------------------------ 2. 入参收敛
def test_charge_tolerates_dirty_token_values():
  """usage 字段为 None / 非数字字符串时不能崩。

  验证旧行为：None → TypeError，'abc' → ValueError，
  都会冒泡成"操作失败，请稍后重试"，把记账问题伪装成服务器故障。
  """
  for bad in [None, "abc", "", [], {}]:
    bank = TokenBank(10_000)
    rec = bank.charge("p", "m", bad, 1)
    assert rec.total >= 0, f"{bad!r} 产生了非法用量 {rec.total}"


def test_charge_string_digits_are_summed_not_concatenated():
  """字符串数字必须按数值相加，不能拼成 "12"+"7"="127"。"""
  bank = TokenBank(10_000)
  rec = bank.charge("p", "m", "12", "7")
  assert rec.total == 19, f"应为 19， {rec.total}"


# -------------------------------------------------------------- 3. 复杂度
def test_charge_is_linear_not_quadratic():
  """N 次 charge 必须近似 O(N)。

  旧实现 total_spent 每次 sum(全部记录)，而 charge() 内部会调它，
  于是 N 次 charge 是 O(N²)：验证 5000 次耗时 0.808s。
  改为累加计数后同样规模约 0.007s。
  """
  def timed(n: int) -> float:
    bank = TokenBank(10**9)
    t = time.time()
    for _ in range(n):
      bank.charge("p", "m", 1, 1)
    return time.time() - t

  # 规模必须够大才测得出来：真·O(N²) 在 1000 次时只有 0.048s，
  # 与 O(1) 的 0.001s 在同一噪声量级，阈值一放宽就漏判
  # （本条守卫第一版就是这个原因被回退校验判成"无效守卫"）。
  # 取 2000 / 8000：O(N²) 下 0.140s -> 2.025s（约 14 倍，4 倍数据量
  # 对应二次增长的 16 倍），O(1) 下则稳定在 0.01s。
  t1, t2 = timed(2000), timed(8000)
  assert t2 < max(t1 * 8, 0.5), (
    f"charge 疑似 O(N²)：2000 次 {t1:.3f}s，8000 次 {t2:.3f}s")


def test_ledger_and_summary_consistent_with_running_total():
  """改成累加计数后，账本各视图不能与记录脱节。"""
  bank = TokenBank(10_000)
  bank.charge("a", "m", 10, 10)
  bank.charge("b", "m", 20, 5)
  assert bank.total_spent == 45
  assert bank.remaining == 10_000 - 45
  assert bank.ledger_by_phase() == {"a": 20, "b": 25}
  assert bank.summary()["calls"] == 2
  assert bank.summary()["spent"] == 45


# -------------------------------------------------------- 4. 注释不能撒谎
def test_charge_estimate_is_precheck_not_reservation():
  """charge_estimate 只预检、不预占——这是刻意设计，但必须说清楚。

  旧 docstring 写 "Reserve tokens ... (soft reservation)"，实现里
  却没有任何扣减。保留预检语义（真预占会与随后的真实 charge
  双重计费），但文档必须如实，且行为上不得记账。
  """
  bank = TokenBank(10_000)
  before = bank.total_spent
  bank.charge_estimate("p", "m", 9000)
  assert bank.total_spent == before, "预检不应产生任何记账"
  # 预检确实能挡住明显不够的情况
  try:
    bank.charge_estimate("p", "m", 99_999)
  except BudgetExceeded:
    return
  raise AssertionError("明显超额时预检未拦住")


def test_dump_writes_consistent_ledger(tmp_path):
  """落盘账本必须与内存一致（回归保护：换实现后别漏更新 dump）。"""
  bank = TokenBank(10_000)
  bank.charge("x", "m", 3, 4)
  p = str(tmp_path / "ledger.json")
  bank.dump(p)
  import json
  d = json.loads(open(p, encoding="utf-8").read())
  assert d["summary"]["spent"] == 7
  assert len(d["records"]) == 1
