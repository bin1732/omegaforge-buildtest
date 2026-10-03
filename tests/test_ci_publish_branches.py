"""回传分支写法守卫自身的守卫。

盯两类失效：
1. 判定恒真——任何工作流都判通过，等于没查；
2. 合规写法被误判为不合规——会让实际工作流无法通过，进而被整条删掉。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_ci_publish_branches.py"


def _run(workflow: str) -> subprocess.CompletedProcess:
    d = tempfile.mkdtemp(prefix="of_pubbr_")
    wf = Path(d) / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "build.yml").write_text(workflow, encoding="utf-8")
    env = dict(os.environ)
    env["REPO_ROOT"] = d
    return subprocess.run([sys.executable, str(SCRIPT)],
                          env=env, capture_output=True, text=True, timeout=120)


def _rc(workflow: str) -> int:
    return _run(workflow).returncode


BAD_REBUILD = """
      - name: Publish installer to artifacts branch
        run: |
          git checkout -B artifacts
          mkdir -p installer
"""

BAD_NO_FETCH = """
      - name: Publish installer to artifacts branch
        run: |
          git checkout --orphan artifacts
          mkdir -p installer
"""

GOOD = """
      - name: Publish installer to artifacts branch
        run: |
          if git fetch origin artifacts:artifacts 2>/dev/null; then
            git checkout artifacts
          else
            git checkout --orphan artifacts
          fi
          mkdir -p installer
"""

# 注释里引用旧写法是常见情形（说明为什么要改），不应被判为不合规
GOOD_WITH_COMMENT = """
      - name: Publish installer to artifacts branch
        run: |
          # 不能用 git checkout -B artifacts：会混入源码树
          if git fetch origin artifacts:artifacts 2>/dev/null; then
            git checkout artifacts
          else
            git checkout --orphan artifacts
          fi
"""

NO_BRANCH = """
      - name: Build
        run: |
          npm run build
"""


def test_missing_yaml_does_not_silently_pass():
    """缺 pyyaml 时不得整条静默失效。

    except ImportError: return [] 曾让这一条在 runner 上全部判通过（本地装了
    pyyaml 所以本地永远复现不了），症状只有"一直绿"——与本守卫要抓的失效同型。
    缺依赖必须改走按原文判的退路，宁可多报也不静默放行。
    """
    src = (SCRIPT).read_text(encoding="utf-8")
    self = None
    import re as _re
    m = _re.search(r"except ImportError:\n(\s+)return\s+(\S+)", src)
    assert m, "未找到 ImportError 分支，口径已变，须重定位"
    assert m.group(2) != "[]", (
        "缺 pyyaml 时返回空会让这一条整条失效；须改为按原文判的退路")


def test_rebuild_is_rejected():
    assert _rc(BAD_REBUILD) == 1, "重建回传分支必须判失败"


def test_without_fetch_is_rejected():
    # 只有孤儿创建、没有从远端继续时，已有内容会被覆盖
    assert _rc(BAD_NO_FETCH) == 1, "未从远端已有分支继续必须判失败"


def test_compliant_is_accepted():
    p = _run(GOOD)
    assert p.returncode == 0, f"合规写法被误判：{p.stdout}"


def test_comment_mention_is_not_treated_as_usage():
    p = _run(GOOD_WITH_COMMENT)
    assert p.returncode == 0, f"注释中的旧写法不应被判为实际写法：{p.stdout}"


def test_unrelated_workflow_is_accepted():
    assert _rc(NO_BRANCH) == 0, "未涉及回传分支的工作流应判通过"


def test_real_workflow_passes():
    p = subprocess.run([sys.executable, str(SCRIPT)],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, f"实际工作流未通过：{p.stdout}"


# ---- 清索引（管"结果"，前两条只管"写法"）----

PRUNE_OK = """
jobs:
  build:
    steps:
      - name: Publish installer to artifacts branch
        run: |
          git fetch origin artifacts:artifacts
          git checkout artifacts
          git rm -r --cached . --quiet 2>/dev/null
          git add -f installer
"""

PRUNE_MISSING = """
jobs:
  build:
    steps:
      - name: Publish installer to artifacts branch
        run: |
          git fetch origin artifacts:artifacts
          git checkout artifacts
          git add -f installer
"""

PRUNE_AFTER_ADD = """
jobs:
  build:
    steps:
      - name: Publish installer to artifacts branch
        run: |
          git fetch origin artifacts:artifacts
          git checkout artifacts
          git add -f installer
          git rm -r --cached . --quiet 2>/dev/null
"""

PRUNE_ONLY_IN_COMMENT = """
jobs:
  build:
    steps:
      - name: Publish installer to artifacts branch
        run: |
          git fetch origin artifacts:artifacts
          git checkout artifacts
          # git rm -r --cached . 只写在说明里，不算实际执行
          git add -f installer
"""


def test_prune_present_is_accepted():
    assert _rc(PRUNE_OK) == 0


def test_missing_prune_is_rejected():
    """只从远端继续、不用 -B，仍可能带着历史污染。

    分支一旦混入过源码树，它此后每一次提交都会带着，而前两条判定照旧成立。
    """
    r = _run(PRUNE_MISSING)
    assert r.returncode == 1, "未清索引必须判失败"
    assert "清索引" in r.stdout


def test_prune_after_add_is_rejected():
    """顺序反了等于没清。"""
    r = _run(PRUNE_AFTER_ADD)
    assert r.returncode == 1, "清索引在 add 之后必须判失败"
    assert "顺序" in r.stdout


def test_prune_only_in_comment_is_rejected():
    """注释里引用不算执行，否则守卫会因注释而误放。"""
    r = _run(PRUNE_ONLY_IN_COMMENT)
    assert r.returncode == 1, "只在注释里出现必须判失败"


def test_unlocatable_steps_are_not_silently_accepted():
    """工作流改结构后定位不到步骤时，不得静默判通过。

    静默放行与这一条要抓的失效同型：换键名、改用可复用工作流都会让它整体
    失效，而症状只有"一直绿"。
    """
    fragment = """
      - name: Publish installer to artifacts branch
        run: |
          git fetch origin artifacts:artifacts
          git checkout artifacts
          git add -f installer
"""
    r = _run(fragment)
    assert r.returncode == 1, "定位不到步骤时必须判失败，不能当作通过"
    assert "定位" in r.stdout
