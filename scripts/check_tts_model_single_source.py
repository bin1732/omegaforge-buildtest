"""TTS 主模型文件名只许在 voice/spec.py 定义一次。

## 为什么需要这条守卫

换用体积更小的量化版主模型时，需要改文件名。若文件名在下载清单、
运行时需求、引擎加载各写一份，改一处只动一处：

  · 下载器按自己那份清单落盘（下的是新版）
  · 加载按另一份文件名去找（与落盘的那份不一致）
  · 结果是"下载成功、界面仍显示未安装"，而两侧代码单独看都没错

更麻烦的是这类漂移没有任何症状指向它：接口全绿、status 正确、
下载器也能跑通，只有用户启不动。

因此文件名只允许出现在 voice/spec.py（唯一定义处），其余模块必须引用
spec.TTS_MODEL_FILE。这条守卫按 AST 的字符串常量判定——注释里提到同名
文字不算违规，否则正在说明这条规则的注释会被判成违规，守卫在正确实现
上变红，而人因此倾向于把它整条删掉。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VOICE_DIR = REPO / "omegaforge" / "voice"
OWNER = "spec.py"

#: 主模型文件名的两种形态：fp32 与 int8 量化版。
NAMES = {"model.onnx", "model.int8.onnx"}


def _string_constants(path: Path) -> list[tuple[int, str]]:
    """(行号, 字符串值)：只取真实的字符串常量，注释不在其中。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        # 解析不了必须报错：静默返回空集会退化成"全部合规"。
        return [(0, f"<无法解析源码: {path.name}>")]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((getattr(node, "lineno", 0), node.value))
    return out


def scan() -> list[str]:
    """返回违规描述列表。空列表表示合规。"""
    if not VOICE_DIR.is_dir():
        # 目录不存在必须报错：否则路径写错时它退化成恒真。
        return [f"找不到语音模块目录：{VOICE_DIR}"]
    bad = []
    for path in sorted(VOICE_DIR.glob("*.py")):
        if path.name == OWNER:
            continue
        for lineno, value in _string_constants(path):
            if value in NAMES:
                bad.append(f"{path.name}:{lineno} 出现主模型文件名 "
                           f"{value!r}（应引用 spec.TTS_MODEL_FILE）")
    return bad


def main() -> int:
    bad = scan()
    if bad:
        print("FAIL TTS 主模型文件名不得在 spec.py 之外出现：")
        for b in bad:
            print("  " + b)
        return 1
    print("OK TTS 主模型文件名单一定义")
    return 0


if __name__ == "__main__":
    sys.exit(main())
