"""前端中文化标签的覆盖守卫（真实响应驱动）。

背景——`frontend/src/lib/labels.ts` 是"英文键名 → 中文标签"的映射表，
`Structured` 组件靠它把后端 JSON 渲染成中文。它失效的形态**不是报错**，
而是某个键没收录时原样显示英文，用户看到英文字段名
这类开发术语。

验证到的三类失效（本次改动）：

1. **守卫脚本从不执行**。`scripts/check_labels.py` 逻辑是对的（缺字段时
  退出码 1），但**没有任何测试或 CI 步骤调用它**。结果 16 个字段长期
  未翻译，包括 `contaminated_cases`、`debiased_cases` 这些可信度字段。

2. **字段名漂移潜入映射表**。表里 Run/Job 段写的是 `created_at` 与
  `phases`，而真实 `/api/runs` 返回的是 `created_ts` 与 `phase`/`progress`。
  同一个文件里 Genome 段用的却是正确的 `created_ts`——**自相矛盾**。
  漂移的后果是"创建时间"这一栏永远查不到对应标签。

3. **property 字段查不到**。结论成立 / 裁判自证 /
  出题侧自证是 `DistillReport` 的 property，不在 dataclass
  字段列表里，所以 `check_labels.py` **永远报不出它们**。而这三项恰恰
  决定「更强」这个结论能否成立。

本文件的占位思路：不再只读 dataclass 定义，而是**起真后端拿真实响应**，
用真实出现的键去反查标签表。
"""

from __future__ import annotations

import json
import re
import subprocess
import shutil
import tempfile
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LABELS_TS = ROOT / "frontend" / "src" / "lib" / "labels.ts"
CHECK_SCRIPT = ROOT / "scripts" / "check_labels.py"

# 确认后端**从不返回**的旧字段名（标签表里出现过）。
# 之所以单列一组占位，是因为"把错的名字加回来"不会让任何现有测试变红，
# 只会让界面某个栏目重新变空白——属于静默回退。
_STALE_KEYS = ("created_at", "phases")

# report / genome 的真实键（跑通一次 distill 后 GET /api/report/<id>
# 与 /api/genome/<id> 所得）。跑一次蒸馏耗时数十秒，不适合放进常规测试，
# 故固化为快照，并在用例文档里写明来源。
_REPORT_KEYS_SNAPSHOT = {
  "ablation", "answer_model", "arena_cases", "baseline_comparable",
  "baseline_kind", "baseline_note", "baseline_score", "best_generation",
  "claim_valid", "contaminated_cases", "debiased_cases", "eval_set_cases",
  "eval_set_fingerprint", "eval_set_source", "evolution_notes",
  "exam_self_authored", "final_score", "generation", "generations_run",
  "judge_model", "judge_reasons", "question_model", "question_source",
  "rolled_back", "rubric_source", "self_certified", "source_signals",
  "trust_note", "verdict",
}
_GENOME_KEYS_SNAPSHOT = {
  "arena_best_score", "arena_generation", "arena_history",
  "baseline_tokens_per_task", "created_ts", "est_system_tokens", "id",
  "lineage", "mission_one_liner", "name", "persona_genes",
  "schema_version", "source_fingerprint", "system_prompt", "tool_genes",
  "tools", "upgrade_genes", "workflow", "workflow_genes",
}


def _label_keys() -> set[str]:
  """从 labels.ts 提取映射表键名（先剥离注释，避免把注释里的示例键算进去）。"""
  src = LABELS_TS.read_text(encoding="utf-8")
  src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)   # 块注释
  src = re.sub(r"//[^\n]*", "", src)          # 行注释
  return set(re.findall(r"^\s{2}([a-z_][a-z0-9_]*)\s*:\s*['\"]", src, flags=re.M))


# ---------------------------------------------------------------
# 1. 既有脚本必须真的在跑，且通过
# ---------------------------------------------------------------

def test_check_labels_script_passes():
  """`scripts/check_labels.py` 必须退出码 0。

  它缺少该约束时**未被任何测试调用**，所以长期处于"能报错但没人看"的状态。
  这条占位把它接进回归。
  """
  assert CHECK_SCRIPT.exists(), f"守卫脚本缺失: {CHECK_SCRIPT}"
  r = subprocess.run([sys.executable, str(CHECK_SCRIPT)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180)
  assert r.returncode == 0, (
    "存在未翻译字段（会原样显示英文给用户）：\n" + (r.stdout or r.stderr)[:1500])


def test_check_labels_script_can_fail():
  """脚本必须**能**报错——不只是"跑起来退出码 0"。

  只断言退出码 0 守不住最坏的那种失效：脚本不再报任何缺失字段（判定被
  摘掉或字段源被清空），退出码照样是 0，于是长期处于"全绿但什么也没
  查"的状态。缺字段那一侧必须单独验。

  做法：复制一份 `labels.ts` 到临时文件、删掉一个真实字段的标签，把
  脚本的标签表路径指向它，退出码必须为 1。不动仓库里的文件。
  """
  import importlib.util

  spec = importlib.util.spec_from_file_location("check_labels_under_test",
                         CHECK_SCRIPT)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)          # type: ignore[union-attr]

  # 锚点取 persona_genes 而不是 created_ts：后者另有守卫拿它当注入点，
  # 用它会让两条用例互相牵连，撤一处的修复会连坐另一处。
  src = LABELS_TS.read_text(encoding="utf-8")
  assert "  persona_genes: '人格基因'," in src, \
    "锚点失效：labels.ts 里没有 persona_genes"
  stripped = src.replace("  persona_genes: '人格基因',\n", "", 1)

  tmpdir = tempfile.mkdtemp(prefix="labels_probe_")
  probe = Path(tmpdir) / "labels.ts"
  probe.write_text(stripped, encoding="utf-8")
  try:
    mod.TS_LABELS = str(probe)
    assert mod.main() != 0, (
      "标签表缺了真实字段却退出码 0：脚本已查不出缺失，"
      "「脚本通过」不再代表「字段都翻译了」")
  finally:
    mod.TS_LABELS = str(LABELS_TS)
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------
# 2. 真实响应里出现的键，必须有中文标签
# ---------------------------------------------------------------

_TEST_PORT = 8871 # 固定端口：按 path 做 hash 取端口会受 PYTHONHASHSEED
          # 随机化影响，不同用例可能撞到同一个端口，是脆弱设计。


def _srv_main(tmp_home, port):
  """后端进程入口。

  必须是**模块级**函数：Windows 的默认启动方式是 spawn，子进程要重新导入
  本模块并把目标 pickle 过去，写在 fixture 内的局部函数无法被 pickle
  （Can't pickle local object），表现为整批用例 error——而 error 与"标签
  缺失"在汇总里是两回事，很容易被读成界面真的缺翻译。
  """
  import os
  os.environ["OMEGAFORGE_HOME"] = tmp_home
  os.environ["MOCK"] = "1"
  sys.path.insert(0, str(ROOT))
  import omegaforge.server as s
  s.serve("127.0.0.1", port)


@pytest.fixture(scope="module")
def backend():
  """起一次真实后端，模块内所有用例复用。

  缺少该约束时按 path 各起一个（端口由 hash 决定），结果端口冲突 + 进程互相
  干扰，7 个用例报的缺失字段与单独跑时并不一致——是测试基建自己的问题。
  """
  import multiprocessing as mp
  # 独立临时 home：缺少该约束时用固定的 /tmp/labels_guard_home，跨次运行残留会
  # 让 /api/runs 之类的端点返回内容不同——整批跑失败、单独跑却通过。
  # 这是本项目反复栽的"全局状态污染"，测试环境必须每次全新。
  tmp_home = tempfile.mkdtemp(prefix="labels_guard_")
  p = mp.Process(target=_srv_main, args=(tmp_home, _TEST_PORT), daemon=True)
  p.start()
  body = None
  for _ in range(60):
    try:
      body = _get(_TEST_PORT, "/api/status", timeout=3)
      break
    except Exception:
      time.sleep(0.5)
  if body is None:
    p.terminate()
    pytest.fail("后端未能在预期时间内就绪")
  try:
    yield _TEST_PORT
  finally:
    p.terminate()
    shutil.rmtree(tmp_home, ignore_errors=True)


def _get(port: int, path: str, timeout: int = 10):
  r = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
  with urllib.request.urlopen(r, timeout=timeout) as resp:
    return json.loads(resp.read())


def _keys_at_depth(obj, depth: int) -> set[str]:
  """收集 JSON 对象的键，只下钻到指定深度。

  深度受限是刻意的：`meta`/`by_model`/`timeline` 这类容器里的键是
  动态的（模型名、时间戳），要求它们有标签会造成假失败。
  """
  out: set[str] = set()
  if depth <= 0 or not isinstance(obj, dict):
    return out
  for k, v in obj.items():
    out.add(k)
    if isinstance(v, dict):
      out |= _keys_at_depth(v, depth - 1)
    elif isinstance(v, list) and v and isinstance(v[0], dict):
      out |= _keys_at_depth(v[0], depth - 1)
  return out


@pytest.mark.parametrize("path", [
  "/api/status", "/api/runs", "/api/providers", "/api/conversations",
  "/api/tasks/list", "/api/kb/list", "/api/wiki/list", "/api/skills/list",
  "/api/tools/permissions", "/api/voice/status", "/api/personas",
])
def test_real_endpoint_keys_are_labeled(backend, path):
  """真实后端响应里出现的键，必须在标签表里有中文名。

  `/api/usage` 不在此列：它的 `by_model` / `by_phase` / `timeline`
  以模型名和时间戳为键，属动态键，不应当要求翻译。

  复用模块级 `backend` fixture：各用例各起一个后端时，端口由
  `hash(path)` 决定且受 PYTHONHASHSEED 随机化影响，会发生撞端口，
  导致报出的缺失字段与单独跑时不一致——那是测试基建自己的问题，
  不是产品问题。
  """
  data = _get(backend, path, timeout=15)

  keys = _keys_at_depth(data, depth=2)
  labels = _label_keys()
  missing = sorted(k for k in keys if k not in labels)
  assert not missing, (
    f"{path} 返回的这些键没有中文标签，会原样显示英文：{missing}")


# ---------------------------------------------------------------
# 3. report / genome 快照（含 property 字段）
# ---------------------------------------------------------------

def test_report_snapshot_keys_are_labeled():
  """蒸馏报告的键必须全部有标签——含三个 property 可信度字段。

  这三项（结论成立 / 裁判自证 / 出题侧自证）
  不在 dataclass 字段列表里，`check_labels.py` 覆盖不到，只能靠快照守。
  它们决定「更强」能否成立，漏译的话会以英文直出给用户。
  """
  labels = _label_keys()
  # 快照为空时差集恒为空，用例会永远通过却什么也没查——必须先自证非空。
  assert len(_REPORT_KEYS_SNAPSHOT) >= 10, (
    f"报告字段快照只有 {len(_REPORT_KEYS_SNAPSHOT)} 个，疑似被清空或截断")
  missing = sorted(_REPORT_KEYS_SNAPSHOT - labels)
  assert not missing, f"报告字段缺少中文标签：{missing}"


def test_genome_snapshot_keys_are_labeled():
  labels = _label_keys()
  # 同上：空快照让差集恒为空，用例会退化成永远通过。
  assert len(_GENOME_KEYS_SNAPSHOT) >= 10, (
    f"基因组字段快照只有 {len(_GENOME_KEYS_SNAPSHOT)} 个，疑似被清空或截断")
  missing = sorted(_GENOME_KEYS_SNAPSHOT - labels)
  assert not missing, f"基因组字段缺少中文标签：{missing}"


# ---------------------------------------------------------------
# 4. 漂移字段名不得回潮
# ---------------------------------------------------------------

def test_stale_field_names_absent():
  """标签表里不得出现后端不返回的旧字段名。

  把 `created_at` / `phases` 加回来不会让任何现有测试变红，只会让
  界面相应栏目重新变空白——属静默回退，必须单独守。
  """
  labels = _label_keys()
  present = [k for k in _STALE_KEYS if k in labels]
  assert not present, (
    f"标签表含后端不返回的字段名，会让对应栏目永久空白：{present}")


def test_created_ts_labeled_and_used_consistently():
  """真实字段 `created_ts` 必须有标签，且与页面读取的名字一致。

  页面（RunsPage / ArenaPage / GenomePage）读的是 `created_ts`；
  若标签表只有旧名，时间栏会既无数据也无标签。
  """
  labels = _label_keys()
  assert "created_ts" in labels, "created_ts 缺少中文标签"
  for rel in ("src/pages/RunsPage.tsx", "src/pages/ArenaPage.tsx",
        "src/pages/GenomePage.tsx"):
    src = (ROOT / "frontend" / rel).read_text(encoding="utf-8")
    assert "created_ts" in src, f"{rel} 未使用真实字段 created_ts"
    assert "created_at" not in src, f"{rel} 仍在读已废弃的 created_at"
