"""检查脚本：对照提示词的"够不够格"判定，中英文是否同一把尺子。

背景：core/validate.py 提供了 text_weight / looks_enough（中文按字计、
西文按 5 字符一词计），用于解决"等长门槛系统性拒绝中文"。但覆盖度
检查脚本显示这两个函数**全仓无调用者**——修复写了，没接通。

本检查脚本用真实调用验证：同一信息量的中英文提示词，是否被区别对待。

运行：  python3 probes/probe_prompt_threshold.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omegaforge.core.validate import looks_enough, text_weight  # noqa: E402
from omegaforge.domain.baseline import (  # noqa: E402
    MIN_PROMPT_CHARS, build_baseline,
)


class _Sig:
    name_hint = "助手"
    prompt_candidates: list[str] = []


def main() -> int:
    zh = "你是一位资深代码审查专家，请检查代码中的并发与资源泄漏问题，并给出可执行的修改建议。"  # 41 字
    en = ("You are a senior code review expert. Please check the code for "
          "concurrency and resource leak issues, and give actionable fixes.")  # 125 字符

    print(f"中文 prompt  {len(zh)} 字符  信息量权重 {text_weight(zh):.1f}")
    print(f"英文 prompt  {len(en)} 字符  信息量权重 {text_weight(en):.1f}")
    print(f"门槛 MIN_PROMPT_CHARS = {MIN_PROMPT_CHARS}")
    print()

    print("=== 用户显式提供分支 ===")
    for tag, p in (("中文", zh), ("英文", en)):
        b = build_baseline(_Sig(), provided_prompt=p)
        print(f"  {tag}: kind={b.kind}  comparable={getattr(b, 'comparable', None)}")
        print(f"        note={getattr(b, 'note', '')[:70]}")

    print()
    print("=== 源材料提取分支（用户不填时的默认路径）===")
    for tag, p in (("中文", zh), ("英文", en)):
        s = _Sig()
        s.prompt_candidates = [p]
        b = build_baseline(s)
        print(f"  {tag}: kind={b.kind}")
        print(f"        note={getattr(b, 'note', '')[:70]}")

    print()
    print("=== 防修过头：短文本必须仍被拒 ===")
    for p in ("hi", "你好", "你是一位助手"):
        b = build_baseline(_Sig(), provided_prompt=p)
        print(f"  {p!r}: kind={b.kind}  weight={text_weight(p):.1f} "
              f"looks_enough={looks_enough(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
