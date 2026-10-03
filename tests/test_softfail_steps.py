"""软失败步骤守卫的用例。

每条对应用户可见的一种失效形态。

撤除形态必须成对出现：把修好的写法改回失效写法，对应用例必须转红。
只留"修好后通过"的用例，撤掉修复它也通过，那层校验等于不存在。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_softfail_steps as guard  # noqa: E402

BUILD_YML = os.path.join(ROOT, ".github", "workflows", "build.yml")

STEP_OK = """\
      - name: Some soft step
        shell: bash
        run: |
          set -o pipefail
          set +e
          mkdir -p ci_diag
          bash scripts/x.sh 2>&1 | tee ci_diag/x.log
          rc=${PIPESTATUS[0]}
          if [ "$rc" -ne 0 ]; then echo "x rc=$rc" >> ci_diag/soft-fail.txt; fi
          exit 0
"""

STEP_BAD = STEP_OK.replace("          set +e\n", "")


def _write(tmp_path, body):
    p = tmp_path / "w.yml"
    p.write_text("jobs:\n  build:\n    steps:\n" + body, encoding="utf-8")
    return str(p)


def test_real_build_yml_passes():
    assert os.path.isfile(BUILD_YML), "工作流文件缺失不得判通过"
    assert guard.check_workflow(BUILD_YML) == []


def test_real_build_yml_has_soft_fail_steps():
    """编排里必须真的有软失败步骤，否则前一条用例在空集上恒绿。"""
    with open(BUILD_YML, encoding="utf-8") as f:
        body = f.read()
    soft = guard._step_blocks(body)
    hit = [n for n, b in soft if guard.is_soft_fail(b)]
    assert hit, "build.yml 里找不到往 soft-fail.txt 追加的软失败步骤"


def test_missing_set_e_is_flagged(tmp_path):
    """撤除形态：撤掉 set +e，必须点名到步骤。"""
    reasons = guard.check_workflow(_write(tmp_path, STEP_BAD))
    assert reasons, "撤掉 set +e 后守卫仍判通过 —— 恒真的守卫比没有更坏"
    assert "Some soft step" in reasons[0]


def test_set_e_present_passes(tmp_path):
    assert guard.check_workflow(_write(tmp_path, STEP_OK)) == []


def test_comment_mention_does_not_count(tmp_path):
    """注释里提到 set +e 不算：按文本命中会让说明规则的注释骗过守卫。"""
    body = STEP_BAD.replace(
        "          set -o pipefail\n",
        "          set -o pipefail\n          # 记得 set +e，否则 errexit 会吃掉 exit 0\n",
    )
    reasons = guard.check_workflow(_write(tmp_path, body))
    assert reasons, "注释里的 set +e 被当成真实指令，守卫会在失效写法上判通过"


def test_no_soft_fail_step_is_failure(tmp_path):
    """一个都没找到不得判通过：路径写错或改名时守卫不能永久绿着。"""
    body = STEP_OK.replace("soft-fail.txt", "other.txt")
    reasons = guard.check_workflow(_write(tmp_path, body))
    assert reasons and "空结果不得判通过" in reasons[0]


def test_missing_file_is_failure(tmp_path):
    reasons = guard.check_workflow(str(tmp_path / "nope.yml"))
    assert reasons and "不存在" in reasons[0]
