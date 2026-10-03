"""语音模型体积下限：按条目取值，且只有一处定义。

## 守的是什么

下限原本是给模型文件定的：占位（0 字节）与截断（几十字节）的 onnx 会让
界面显示"可用"，用户一点合成就崩，而报错来自 onnx 内部，指不到下载环节。

但同一份清单里还有词表与词典这类真的小文件——vits-melo 的 tokens.txt 只有
655 字节。对所有条目统一套用模型文件的下限，后果分两处：

  · 打包内置：tokens.txt 下载成功却被判"不是有效文件"，构建直接失败，
    而报错看起来是镜像或网络的问题；
  · 运行时：文件明明在，status 仍报缺失，界面显示"模型未下载完整"。

两处的症状都不指向真实原因，且重试多少次都不会变。
"""
from __future__ import annotations

import io
import os
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from omegaforge.voice import spec as voice_spec
from omegaforge.voice.model_size import MODEL_MIN_BYTES

#: 允许直接引用 MODEL_MIN_BYTES 的文件：定义处与按条目分派处。
#: 其余文件必须走 voice_spec.min_bytes_for，否则同一条判据会出现两份。
ALLOWED = {"omegaforge/voice/model_size.py", "omegaforge/voice/spec.py"}

#: 必须走 min_bytes_for 的判定点。
JUDGE_SITES = ["omegaforge/voice/asr.py", "omegaforge/voice/tts.py",
               "omegaforge/voice/fetch.py", "scripts/fetch_voice_assets.py",
               "scripts/check_voice_bundle.py"]


def test_model_file_uses_full_floor():
    assert voice_spec.min_bytes_for("model.onnx") == MODEL_MIN_BYTES
    assert voice_spec.min_bytes_for("encoder-epoch-99-avg-1.int8.onnx") == MODEL_MIN_BYTES


def test_small_vocab_not_killed_by_model_floor():
    """真实词表（655 字节）必须能过，且 0 字节仍算缺失。"""
    assert voice_spec.min_bytes_for("tokens.txt") <= 655
    assert voice_spec.min_bytes_for("tokens.txt") > 0, "0 字节不得算齐备"
    assert voice_spec.min_bytes_for("lexicon.txt") <= 655


def _code_only(text: str) -> str:
    """去掉注释与文档字符串里的内容，只留真实代码。

    按字面量扫会把正在说明这条约束的注释判成违规，守卫在正确实现上变红，
    而人因此倾向于把守卫整条删掉——那正是"错误页被当成模型装好"得以长期
    存在的原因。
    """
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


@pytest.mark.parametrize("rel", JUDGE_SITES)
def test_judge_sites_use_single_definition(rel):
    path = ROOT / rel
    assert path.is_file(), f"判定点不存在：{rel}（路径写错时不得静默放行）"
    code = _code_only(path.read_text(encoding="utf-8"))
    assert "MODEL_MIN_BYTES" not in code, (
        f"{rel} 直接引用了 MODEL_MIN_BYTES，须改用 voice_spec.min_bytes_for："
        "同一条判据出现两份字面量时，改一侧不会牵动另一侧，两侧单独看都成立")


def test_allowed_files_still_define_it():
    for rel in ALLOWED:
        assert (ROOT / rel).is_file(), f"定义处缺失：{rel}"
