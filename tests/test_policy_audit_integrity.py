#!/usr/bin/env python3
"""权限级别变更的审计完整性守卫。

## 背景

`Policy.set_mode()` 是全产品唯一的"交出免确认权"入口：从 变更前确认
提到 完全访问 之后，高风险操作（终端命令）不再逐次询问。

`audit_write()` 原本吞掉全部 OSError，`set_mode()` 外层又用
`except Exception: pass` 再吞一层。双重静默之后，审计写不进去时：

  - 权限级别**照常放宽**（返回值、界面状态都显示成功）
  - 审计文件里**一条都没有**
  - 用户**收不到任何提示**

后果是审计页显示"暂无记录"，而这个空答案无法区分"没人改过权限"
与"改过但全都没记上"。对一个以"可回答谁在何时放宽了门禁"为卖点的
审计闭环，这等于让审计在最需要它的那次操作上失效。

## 修复口径

`audit_write()` 如实返回成败（不阻断调用方，但失败原因进内部日志）；
是否阻断由调用方按敏感度决定：

  - 权限级别变更：必须让用户知情
  - 单次工具执行：不阻断主流程（高频操作，审计失败不应让工具不可用）

## 守卫分层

1. 正常路径：审计成功、返回新级别、审计可查
2. 审计失败：抛可执行的中文提示，且提示必须说清"已生效"
3. 防修过头：审计失败时权限**不回滚**（磁盘写不进审计时，回滚同样
   可能失败，反而造成两处状态不一致）
4. `audit_write` 的返回值必须如实，不能恒为真
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from omegaforge.core.errors import UserError
from omegaforge.tools import policy as P


@pytest.fixture()
def home(monkeypatch):
    d = Path(tempfile.mkdtemp())
    monkeypatch.setenv("OMEGAFORGE_HOME", str(d))
    P._home()  # 触发单例重解析
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_正常路径_审计写入成功且可查(home):
    ps = P.Policy()
    assert ps.set_mode("full") == "full"
    rows = P.audit_read()
    assert len(rows) == 1
    assert rows[0]["to"] == "full"
    assert rows[0]["from"] == "confirm"


def test_审计写入失败时_必须让用户知情(home):
    ps = P.Policy()
    ps.set_mode("auto_edit")
    # 让审计目标变成同名目录：open(..., 'a') 抛 IsADirectoryError（OSError 子类）
    (home / "tools_audit.jsonl").unlink(missing_ok=True)
    (home / "tools_audit.jsonl").mkdir()

    with pytest.raises(UserError) as ei:
        ps.set_mode("full")
    msg = str(ei.value)
    # 必须同时说清"已生效"与"审计没记上"，否则用户会以为权限没改成
    assert "已更新" in msg
    assert "审计" in msg
    assert "完全访问" in msg
    # 不得把内部异常名或路径甩给用户
    assert "IsADirectoryError" not in msg
    assert "jsonl" not in msg


def test_审计失败时_权限级别不回滚(home):
    """防修过头：不能为了让调用方报错就把已落盘的权限改回去。"""
    ps = P.Policy()
    (home / "tools_audit.jsonl").unlink(missing_ok=True)
    (home / "tools_audit.jsonl").mkdir()
    with pytest.raises(UserError):
        ps.set_mode("full")
    # 已生效就是已生效，回滚会造成界面状态与实际权限不一致
    assert ps.mode() == "full"


def test_audit_write_如实返回成败(home):
    assert P.audit_write({"tool": "probe"}) is True
    (home / "tools_audit.jsonl").unlink(missing_ok=True)
    (home / "tools_audit.jsonl").mkdir()
    assert P.audit_write({"tool": "probe"}) is False


def test_审计失败不阻断_单次工具执行路径(home):
    """高频工具审计仍遵循"失败不影响主流程"，只要求如实上报。"""
    (home / "tools_audit.jsonl").unlink(missing_ok=True)
    (home / "tools_audit.jsonl").mkdir()
    # 不抛异常，仅返回 False——调用方自行决定是否阻断
    assert P.audit_write({"tool": "fs_write", "verdict": "allow"}) is False
