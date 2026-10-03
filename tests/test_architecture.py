# -*- coding: utf-8 -*-
"""架构契约测试 —— 把分层依赖方向固化为可执行规则。

为什么需要它（而不是靠文档）：
 架构文档写得再好也会腐化。只有把"谁不能依赖谁"变成每次提交都跑的测试，
 分层才是真的分层，否则只是一组目录名。

四层规则：
   api -> application -> domain <- infrastructure
 - domain     不得依赖 application / infrastructure / api
 - application  可依赖 domain / infrastructure，不得依赖 api
 - infrastructure 可依赖 domain，不得依赖 application / api
 - api      可依赖全部

另加两条外部依赖禁令：
 - domain 层不得 import 任何外部 SDK / Web 框架（httpx / fastapi / openai / requests 等）
 - application 层不得直接 import Web 框架（fastapi / flask / django）
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "omegaforge"

# 目录 -> 层
LAYER = {
  "domain": "domain",
  "core": "domain",
  "distill": "application",
  "chat": "application",
  "skills": "application",
  "tools": "application",
  "agent": "application",
  "llm": "infrastructure",
  "voice": "infrastructure",
  "memory": "infrastructure",
  "server": "api",
  "mcp_server": "api",
  "cli": "api",
}
FORBIDDEN = {
  "domain": {"application", "infrastructure", "api"},
  "application": {"api"},
  "infrastructure": {"application", "api"},
  "api": set(),
}
# 外部依赖禁令（模块名前缀）
BANNED_EXTERNAL = {
  "domain": {"httpx", "fastapi", "flask", "django", "requests", "openai",
        "anthropic", "zhipuai", "dashscope", "aiohttp", "uvicorn"},
  "application": {"fastapi", "flask", "django", "uvicorn"},
}


def _layer_of(mod: str) -> str:
  return LAYER.get(mod.split(".")[0], "?")


def _resolve(pkg_parts: list[str], dots: str, tail: str) -> str:
  up = len(dots) - 1
  base = list(pkg_parts)
  if up > 0:
    base = base[: len(base) - up] if len(base) >= up else []
  if tail:
    base += tail.split(".")
  return ".".join(x for x in base if x)


def _iter_edges():
  """产出 (当前模块, 当前层, 目标模块, 目标层)"""
  for path in sorted(PKG.rglob("*.py")):
    if "__pycache__" in path.parts:
      continue
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[0] == "omegaforge":
      parts = parts[1:]
    if parts and parts[-1] == "__init__":
      parts = parts[:-1]
    cur = ".".join(parts)
    pkg_parts = parts[:-1] if len(parts) > 1 else []
    try:
      tree = ast.parse(path.read_text(encoding="utf8", errors="ignore"))
    except SyntaxError:
      continue
    for node in ast.walk(tree):
      if isinstance(node, ast.ImportFrom) and node.level:
        tgt = _resolve(pkg_parts, "." * node.level, node.module or "")
        if not tgt:
          continue
        yield cur, _layer_of(cur), tgt, _layer_of(tgt), path
      elif isinstance(node, ast.Import):
        for a in node.names:
          if a.name.startswith("omegaforge"):
            tgt = a.name[len("omegaforge."):]
            yield cur, _layer_of(cur), tgt, _layer_of(tgt), path


def test_directory_layer_mapping_is_complete():
  """每个顶层子目录都必须有明确层归属，防止新目录游离在架构之外。"""
  missing = []
  for d in sorted(PKG.iterdir()):
    if d.is_dir() and d.name != "__pycache__":
      if d.name not in LAYER:
        missing.append(d.name)
  assert not missing, f"以下目录未声明层归属，请补进 LAYER: {missing}"


def test_no_illegal_cross_layer_dependency():
  """跨层依赖方向必须合法。"""
  bad = []
  for cur, sl, tgt, tl, path in _iter_edges():
    if sl == tl:
      continue
    if tl in FORBIDDEN.get(sl, set()):
      bad.append(f"[{sl}] {cur} -> [{tl}] {tgt}  ({path.name})")
  assert not bad, "发现非法跨层依赖:\n" + "\n".join(bad)


def test_domain_has_no_external_sdk_dependency():
  """domain 层必须纯净：不得 import 任何外部 SDK / Web 框架。"""
  bad = []
  for cur, sl, tgt, tl, path in _iter_edges():
    if sl not in BANNED_EXTERNAL:
      continue
  for path in sorted(PKG.rglob("*.py")):
    if "__pycache__" in path.parts:
      continue
    rel = path.relative_to(PKG).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
      parts = parts[:-1]
    if not parts:
      continue
    layer = _layer_of(".".join(parts))
    banned = BANNED_EXTERNAL.get(layer)
    if not banned:
      continue
    try:
      tree = ast.parse(path.read_text(encoding="utf8", errors="ignore"))
    except SyntaxError:
      continue
    for node in ast.walk(tree):
      names = []
      if isinstance(node, ast.Import):
        names = [a.name.split(".")[0] for a in node.names]
      elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
        names = [node.module.split(".")[0]]
      for n in names:
        if n in banned:
          bad.append(f"[{layer}] {'.'.join(parts)} import {n}")
  assert not bad, "内层出现外部 SDK / Web 框架依赖:\n" + "\n".join(bad)


def test_layer_graph_is_not_empty():
  """回退校验：审计器必须真的扫到依赖边，否则等于没检查。

  这是防止"规则写错导致 0 违规"的自欺 —— 历史上吃过这个亏。
  """
  edges = [(c, s, t, tl) for c, s, t, tl, _ in _iter_edges() if s != tl]
  assert len(edges) >= 10, (
    f"只扫到 {len(edges)} 条跨层依赖，疑似解析器失效（上次基线 39 条）"
  )
