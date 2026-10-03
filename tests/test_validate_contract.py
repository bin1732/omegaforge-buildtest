"""validate.py 契约守卫：NaN / Infinity 不得穿透成 500，0 不得算未填写。

背景（可复现，非推演）：Python 的 json.loads **默认接受** NaN 与
Infinity（parse_constant 不拦），而 json.dumps 也默认输出它们——
所以任何用 Python json 模块的客户端 / MCP host 都能把这两个字面量
送进来。缺少该约束时的 HTTP 验证：

  POST /api/distill  {"budget": Infinity} -> 500「操作失败，请稍后重试」
  POST /api/voice/tts {"speed": NaN}    -> 500「操作失败，请稍后重试」

回退校验见 scripts/revert_validate.py。
"""
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.core.errors import UserError          # noqa: E402
from omegaforge.core.validate import (as_float, as_int,     # noqa: E402
                   require)

# json.loads 默认接受这三个字面量，这是整条链路的前提
LITERALS = ("NaN", "Infinity", "-Infinity")


@pytest.mark.parametrize("lit", LITERALS)
def test_json_accepts_non_finite(lit):
  """前提守卫：若 Python 哪天不再接受，本文件的其余假设都要重写。"""
  assert not math.isfinite(json.loads('{"x": %s}' % lit)["x"])


@pytest.mark.parametrize("lit", LITERALS)
def test_as_int_rejects_non_finite(lit):
  """int(nan) 抛 ValueError、int(inf) 抛 OverflowError，都会穿透成 500。"""
  payload = json.loads('{"x": %s}' % lit)
  with pytest.raises(UserError):
    as_int(payload, "x", 5)


@pytest.mark.parametrize("lit", LITERALS)
def test_as_float_rejects_non_finite(lit):
  """NaN 的任何比较都为 False，能静默通过 min/max 全部范围校验。"""
  payload = json.loads('{"x": %s}' % lit)
  with pytest.raises(UserError):
    as_float(payload, "x", 5.0)
  with pytest.raises(UserError):
    as_float(payload, "x", 5.0, minimum=0.0, maximum=100.0)


def test_as_float_nan_would_pass_range_checks():
  """为什么必须显式挡：不挡的话 NaN 能越过 minimum 校验落库。"""
  nan = float("nan")
  assert not (nan < 0.0) and not (nan > 100.0)  # 两侧都比较不出问题


def test_require_treats_zero_as_filled():
  """0 与 False 是假值，用 `or ""` 判空会拒绝合法输入。"""
  require({"a": 0, "b": "x"}, ["a", "b"])
  require({"a": False, "b": "x"}, ["a", "b"])


def test_require_still_rejects_blank():
  with pytest.raises(UserError):
    require({"a": "  "}, ["a"])
  with pytest.raises(UserError):
    require({}, ["a"])


@pytest.mark.parametrize("lit", LITERALS)
def test_as_int_message_is_chinese(lit):
  """用户看得到的是这句，不能是英文异常名。"""
  payload = json.loads('{"x": %s}' % lit)
  with pytest.raises(UserError) as e:
    as_int(payload, "x", 5, label="预算")
  assert "预算" in str(e.value)
