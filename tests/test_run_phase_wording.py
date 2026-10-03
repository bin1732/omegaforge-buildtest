#!/usr/bin/env python3
"""运行事件流里的阶段名必须是中文。

## 背景

阶段消息由**后端拼好后落盘**（`run.phase_to(phase, message=...)`），
它不经过前端的任何标签映射。前端 `format.ts` / `labels.ts` 里虽然各有一份
阶段名中文表，但那份只作用于前端自己渲染的字段，管不到已经落进事件流的
文本。

结果是运行详情的事件流里出现"进入阶段 ingest"这类中英夹杂的句子——
而同一时刻进度条上写的是"读取中"，同一页面两套说法。

## 守卫分层

1. **接通守卫**：`phase_text` 必须覆盖引擎实际会进入的全部阶段
   （从 `engine._enter("...")` 的字面量反查，不是照抄一份清单——
   照抄会在引擎新增阶段时静默失效）
2. **覆盖守卫**：进度表里有的阶段，中文表里也必须有
3. **落盘守卫**：真实写入事件流后，消息里不得残留英文阶段标识
4. **防修过头**：未收录的标识原样返回，不做猜测性翻译
"""

import ast
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from omegaforge.core import run as R
from omegaforge.core.run import RunStore, mark_active, phase_text

ENGINE = Path(__file__).resolve().parents[1] / "omegaforge" / "distill" / "engine.py"
SERVER = Path(__file__).resolve().parents[1] / "omegaforge" / "server.py"


def _engine_phases():
    """从引擎源码里反查实际会进入的阶段名。"""
    src = ENGINE.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'self\._enter\("([a-z_]+)"\)', src)))


@pytest.fixture()
def home(monkeypatch):
    d = Path(tempfile.mkdtemp())
    monkeypatch.setenv("OMEGAFORGE_HOME", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_引擎实际进入的阶段_全部有中文说法():
    phases = _engine_phases()
    assert phases, "反查不到任何阶段，守卫本身失效了"
    missing = [p for p in phases if phase_text(p) == p]
    assert not missing, f"这些阶段缺中文说法：{missing}"


def test_进度表里的阶段_中文表也必须有():
    missing = [p for p in R.PHASE_PROGRESS if phase_text(p) == p]
    assert not missing, f"这些阶段缺中文说法：{missing}"


def test_中文说法不含英文阶段标识():
    for p in R.PHASE_PROGRESS:
        t = phase_text(p)
        if t == p:
            continue
        assert not re.search(r"[a-z]{4,}", t), f"“{p}”的中文说法里含英文：{t}"


@pytest.mark.parametrize("phase", ["ingest", "extract", "compress", "synthesize",
                                   "gen_eval", "freeze_eval", "arena", "evolve",
                                   "ablation", "finalize"])
def test_落盘后消息里不残留英文阶段标识(home, phase):
    rs = RunStore()
    r = rs.create(source="probe")
    mark_active(r.id)
    r.phase_to(phase, message=f"进入阶段：{phase_text(phase)}")
    msgs = [e.get("message", "") for e in r.events() if e.get("type") == "phase"]
    assert msgs
    for m in msgs:
        assert not re.fullmatch(r"[a-z_]+", m.strip()), f"消息是裸的阶段标识：{m}"
    # 除"进入阶段："前缀外不得再出现英文单词
    body = msgs[-1].split("：", 1)[-1]
    assert not re.search(r"[a-z]{4,}", body), f"阶段消息残留英文：{msgs[-1]}"


def test_未收录的标识原样返回_不做猜测翻译():
    """防修过头：猜测性翻译会把未知阶段翻成错误中文，比保留原样更糟。"""
    assert phase_text("totally_unknown_phase") == "totally_unknown_phase"


def test_服务端阶段回调确实转了中文():
    """接线守卫：只测 `phase_text` 本身不够——调用方若直接拼阶段标识，
    中文表再全也与事件流无关。而事件消息是后端拼好后落盘的，
    前端那份标签映射管不到它，所以这条接通断了不会有任何别的检查兜住。
    """
    src = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == "on_phase"]
    assert found, "server.py 里找不到阶段回调 on_phase，守卫本身失效了"
    body = "\n".join(
        ast.get_source_segment(src, s) or "" for s in found[0].body)
    assert "phase_text(" in body, (
        "阶段回调没有调用 phase_text，事件消息会把阶段标识原样落盘")
    # 不得出现"只拼 phase 不转中文"的写法
    assert not re.search(r'message\s*=\s*f?"[^"]*\{phase\}', body), (
        "阶段消息里直接内插了阶段标识，未转中文")
