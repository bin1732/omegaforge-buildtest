"""Baseline — 竞技场里的"对照组"。

## 为什么需要这个模块

原实现的对照组是一条编造的弱提示：

    "You are {name}. Answer the task thoroughly."

没有质量条、没有工作流、没有工具约束。蒸馏体（带完整 system_提示词）
与它对比几乎必胜——README 宣称的"可验证地更强（verifiably stronger）"
因此是自我实现的预言：胜负在实验设计阶段就已被决定，而不是被测出来的。

## 正确的对照组

蒸馏的对象是**某个已存在的 agent**。那么公平的对照组只能是
**那个 agent 本身**，即源材料里提取出的原始提示词。

三种情形，必须如实区分并标注，不能混为一谈：

| kind           | 含义                       | 可比性 |
|----------------|----------------------------|--------|
| 源提示词     | 用源 agent 的原始提示词        | 真实可比 |
| `provided`     | 用户显式提供的对照 agent     | 真实可比 |
| 简化对照     | 源里没有提示词，改用简化对照     | **不可比**，仅演示 |

简化对照必须在报告中显著标注。把一个不可比的对照当成"我们更强"的证据，
是这个产品最需要被修掉的地方。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.validate import looks_enough, text_weight


@dataclass
class Baseline:
    """竞技场对照组。"""

    kind: str                    # source_提示词 | provided | 简化对照
    system_prompt: str
    note: str = ""
    source_ref: str = ""         # 来源指纹/路径，便于追溯

    # 可信度：只有真实可比的对照才配得上"可验证更强"这个结论
    @property
    def comparable(self) -> bool:
        return self.kind in ("source_prompt", "provided")

    @property
    def label(self) -> str:
        return {
            "source_prompt": "源 Agent 原始提示词",
            "provided": "用户指定对照",
            "naive": "简化对照（不可比，仅演示）",
        }.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label,
                "comparable": self.comparable, "note": self.note,
                "source_ref": self.source_ref,
                "prompt_chars": len(self.system_prompt)}


# 兜底：源材料里确实没有可提取 提示词 时，明确降级
NAIVE_TEMPLATE = (
    "You are {name}. Complete the task as instructed."
)


# "够格称为一个 agent 的原始 提示词"的最短长度。
# 两条来源路径（用户提供 / 源材料提取）必须用同一把尺子：
# provided 分支直接 return，不判长度，于是
#   build_baseline(sig, provided_prompt="hi") -> kind=provided, comparable=True
# 一个 2 字符的对照组就能让 结论是否成立 变成 True，等于给"可验证地更强"
# 开了一个自己给自己发证的口子。
MIN_PROMPT_CHARS = 120

#: 信息量门槛，与字符门槛等价换算：西文约 5 字符一个词，故 120 字符 ≈ 24 个词。
#: 只按字符数卡门槛会让中文被系统性低估约 5 倍——42 字的完整中文提示词
#: 信息量高于 127 字符的英文，却会被判"不足"。两条来源路径（用户提供 /
#: 源材料提取）必须共用下面这一个判定，分开写必然漏掉其中一处。
MIN_PROMPT_WEIGHT = MIN_PROMPT_CHARS / 5


def _enough(text: str) -> bool:
    """一段提示词是否够格称为"一个 agent"。

    字符数达标，或信息量达标，二者满足其一即可。
    """
    t = (text or "").strip()
    if not t:
        return False
    return len(t) >= MIN_PROMPT_CHARS or looks_enough(t, min_weight=MIN_PROMPT_WEIGHT)


def build_baseline(signals, provided_prompt: str = "") -> Baseline:
    """从源信号构建对照组。

    优先级：用户显式提供 > 源 Agent 原始提示词 > 简化兜底。

    三个入口（命令行 / 服务端 / MCP）都必须能传入对照提示词，否则当用户
    的源材料里提取不到可用的提示词时，对照组必然改用简化对照、结论必然
    不成立，而补救指引"请提供源 Agent 原始提示词"却没有任何地方可填——
    一条走不通的指引比不给更糟。
    """
    # 1) 用户显式提供
    if provided_prompt and provided_prompt.strip():
        given = provided_prompt.strip()
        if not _enough(given):
            return Baseline(
                kind="naive",
                system_prompt=NAIVE_TEMPLATE.format(
                    name=getattr(signals, "name_hint", None) or "an assistant"),
                note=f"用户提供的对照提示词信息量不足：{len(given)} 字符、"
                     f"信息量 {text_weight(given):.1f}"
                     f"（需 {MIN_PROMPT_CHARS} 字符或信息量 {MIN_PROMPT_WEIGHT:.0f}），"
                     f"不足以代表一个 agent，已按不可比处理；"
                     f"此轮胜负不构成'更强'的证据",
                source_ref="user-provided(too-short)",
            )
        return Baseline(
            kind="provided",
            system_prompt=given,
            note="用户显式指定的对照 agent",
            source_ref="user-provided",
        )

    # 2) 源 agent 的原始提示词 —— 取最长的一条（最完整）
    cands = [c for c in (getattr(signals, "prompt_candidates", None) or [])
             if isinstance(c, str) and c.strip()]
    if cands:
        best = max(cands, key=len).strip()
        # 太短的不配称为"原始 提示词"，仍算 简化对照
        if _enough(best):
            return Baseline(
                kind="source_prompt",
                system_prompt=best,
                note=f"取自源材料，共 {len(cands)} 条 prompt 候选中最长的一条"
                     f"（{len(best)} 字符）",
                source_ref=getattr(signals, "fingerprint", "") or "",
            )

    # 3) 兜底：明确标注不可比
    name = getattr(signals, "name_hint", None) or "an assistant"
    # 有候选只是不够格时，不能说成"未包含可提取的 提示词"——那是谎报：
    # 材料里明明有，只是没达标。给错原因会让人去翻材料，而翻多少遍都一样。
    if cands:
        note = (f"源材料中提取到的系统提示词信息量不足"
                f"（{len(best)} 字符、信息量 {text_weight(best):.1f}，"
                f"需 {MIN_PROMPT_CHARS} 字符或信息量 {MIN_PROMPT_WEIGHT:.0f}），"
                f"对照组已改用简化对照；此轮胜负不构成'更强'的证据")
    else:
        note = ("源材料未包含可提取的系统提示词，对照组已改用简化对照；"
                "此轮胜负不构成'更强'的证据")
    return Baseline(
        kind="naive",
        system_prompt=NAIVE_TEMPLATE.format(name=name),
        note=note,
        source_ref=getattr(signals, "fingerprint", "") or "",
    )
