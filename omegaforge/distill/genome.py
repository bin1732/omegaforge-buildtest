"""OmegaForge Agent Genome — the distilled DNA of any agent.

A Genome is a lossless-but-compressed semantic fingerprint of an agent:
what it is, what tools it uses, how it thinks, where it fails, and which
upgrades it should receive. Genomes are JSON, diffable, versionable.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict

from ..core.errors import UserError

GENOME_SCHEMA_VERSION = "1.0"

#: 产物内部字段名 → 面向用户的中文名。
#: 内部标识直接回显（如「缺少必需字段：mission_one_liner」）对用户没有帮助，
#: 只会把开发痕迹暴露在界面上。
_CN_FIELD = {"name": "名称", "mission_one_liner": "使命描述",
             "source_fingerprint": "来源指纹"}


@dataclass
class Genome:
    # identity
    name: str
    mission_one_liner: str
    source_fingerprint: str          # hash/summary of the original agent
    lineage: str = "de-novo"         # distill-of:<source> | de-novo

    # genes
    persona_genes: list[str] = field(default_factory=list)    # compact traits
    tool_genes: list[str] = field(default_factory=list)       # "name(params)"
    workflow_genes: list[str] = field(default_factory=list)   # ordered steps
    upgrade_genes: list[str] = field(default_factory=list)    # OmegaForge boosts

    # compiled artifact
    system_prompt: str = ""
    tools: list[dict] = field(default_factory=list)
    workflow: list[dict] = field(default_factory=list)

    # economics
    est_system_tokens: int = 0
    baseline_tokens_per_task: int = 0

    # evolution state
    arena_generation: int = 0
    arena_best_score: float = 0.0
    arena_history: list[dict] = field(default_factory=list)

    # meta
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_ts: float = field(default_factory=time.time)
    schema_version: str = GENOME_SCHEMA_VERSION

    # ------------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Genome":
        """从 JSON 文本构造 Genome —— 显式 schema 校验，绝不抛 TypeError。

        例如：`cls(**json.loads(raw))` 对三类畸形文件抛
        **TypeError**，而 TypeError 不在任何入口的收口列表里，于是完整
        Python 栈连同本机绝对路径直接甩给用户 ——

            File "/data/workspace/LATEST/omegaforge/distill/genome.py", line 56
              return cls(**json.loads(raw))
            TypeError: Genome.__init__() got an unexpected keyword argument 'hello'

        同样的文件在 CLI 里表现为满屏 traceback，在服务端则被兜成
        「操作失败，请稍后重试」。两种都是错的：这是**用户的文件不对**，
        不是服务器故障，用户能做的只有重新导出。

        为什么在这里修而不是在每个入口各 catch 一次：Genome 是三个入口
        （CLI / server / mcp）共用的唯一加载点，在此收口三处同时受益，
        且不会出现"某个入口忘了接"的漏网 —— 前面几轮反复吃亏的正是
        "每个调用点各修一遍、总有一处没接上"。

        schema_version 高于当前版本单独提示升级，而不是笼统判为损坏：
        那种情况文件是对的，是程序旧了，指引完全不同。
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # ValueError 子类，各入口已有收口；这里不再重复包装
            raise
        if not isinstance(data, dict):
            raise UserError(
                "该文件不是有效的产物文件（内容应为一组字段），"
                "请重新导出后再试")
        ver = data.get("schema_version")
        if isinstance(ver, str) and ver != GENOME_SCHEMA_VERSION:
            try:
                newer = float(ver) > float(GENOME_SCHEMA_VERSION)
            except (TypeError, ValueError):
                newer = False
            if newer:
                raise UserError(
                    f"该文件的结构版本为 {ver}，高于当前支持的"
                    f"{GENOME_SCHEMA_VERSION}，请升级 OmegaForge 后再打开")
        fields = set(cls.__dataclass_fields__)
        missing = [f for f in ("name", "mission_one_liner",
                               "source_fingerprint") if f not in data]
        if missing:
            raise UserError(
                "该文件不是有效的产物文件，缺少必需内容："
                + "、".join(_CN_FIELD.get(f, f) for f in missing)
                + "，请重新导出后再试")
        unknown = [k for k in data if k not in fields]
        if unknown:
            # 不列键名：那是内部标识，列出来对用户没有帮助，只会外泄。
            raise UserError(
                f"该文件不是有效的产物文件，含 {len(unknown)} 项无法识别的"
                "内容，请重新导出后再试")
        return cls(**data)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Genome":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(f.read())

    def diff_summary(self, other: "Genome") -> dict:
        """Human-readable gene diff self -> other (for evolution reports)."""
        def norm(s: str) -> str:
            return s.strip().lower()
        return {
            "added_persona": [g for g in other.persona_genes
                              if norm(g) not in {norm(x) for x in self.persona_genes}],
            "added_tools": [g for g in other.tool_genes
                            if norm(g) not in {norm(x) for x in self.tool_genes}],
            "added_workflow": [g for g in other.workflow_genes
                               if norm(g) not in {norm(x) for x in self.workflow_genes}],
            "added_upgrades": other.upgrade_genes,
        }
