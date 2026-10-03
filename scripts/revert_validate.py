"""校验：逐项撤回修复，确认守卫真的会报错（不是空转）。

回退一律走 git stash/checkout 之外的**定点替换 + 完整还原**：
先把当前修复态复制到临时文件，每轮换一个补丁、跑测试、再从临时文件
还原。这样不会出现"上一版的回退污染下一轮"（我相关部分栽过）。
"""

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())
import io, os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAK = os.path.join(ROOT, ".rev_validate")
os.makedirs(BAK, exist_ok=True)

FILES = ["omegaforge/core/validate.py"]
for f in FILES:
    shutil.copy2(os.path.join(ROOT, f), os.path.join(BAK, os.path.basename(f)))

PYTEST = [sys.executable, "-m", "pytest", "tests/test_validate_contract.py", "-q"]
ENV = dict(os.environ, PYTHONPATH="/data/workspace/.pypkgs")

PATCHES = [
    ("1 as_int 非有限浮点守卫（撤掉）",
     "omegaforge/core/validate.py",
     '        if not math.isfinite(raw):\n            raise UserError(f"{name}需要填写有效数字")\n',
     ''),
    ("2 as_float 非有限守卫（撤掉）",
     "omegaforge/core/validate.py",
     '    if not math.isfinite(val):\n        raise UserError(f"{name}需要填写有效数字")\n',
     ''),
    ("3 require 改回 or \"\" 判空（0 被当未填写）",
     "omegaforge/core/validate.py",
     '    missing = [labels.get(k, k) for k in keys if _blank(payload.get(k))]',
     '    missing = [labels.get(k, k) for k in keys if not str(payload.get(k) or "").strip()]'),
]

results = []
for label, rel, old, new in PATCHES:
    p = os.path.join(ROOT, rel)
    s = io.open(p, encoding="utf-8").read()
    if old not in s:
        results.append((label, "PATCH-NOT-APPLIED"))
        continue
    io.open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
    r = subprocess.run(PYTEST, cwd=ROOT, env=ENV,
                       capture_output=True, text=True, timeout=300)
    tail = [l for l in r.stdout.strip().splitlines() if "passed" in l
            or "failed" in l or "error" in l.lower()]
    results.append((label, (tail[-1] if tail else "?")[:70]))
    # 完整还原，避免跨轮污染
    shutil.copy2(os.path.join(BAK, os.path.basename(rel)), p)

# 确认已还原
r = subprocess.run(PYTEST, cwd=ROOT, env=ENV, capture_output=True,
                   text=True, timeout=300)
restored = [l for l in r.stdout.strip().splitlines() if "passed" in l or "failed" in l]

print("=" * 68)
print("反向验证：撤掉修复 -> 守卫必须报错")
print("=" * 68)
for label, res in results:
    print(f"  {label:44s} {res}")
print("-" * 68)
print("  还原后:", restored[-1] if restored else "?")
shutil.rmtree(BAK, ignore_errors=True)

DEAD = """锚点没命中与"撤掉后用例没红"都判失败：前者意味着校验点根本没
执行，而它打印出来只占一行，与真跑过的输出一样。收集阶段报错同样不可采
信——那时用例一条没跑，与"用例真红"在退出码上同为非零。
"""


def _bad(res: str) -> bool:
    import re as _re
    if "NOT-APPLIED" in res:
        return True
    if "error" in res.lower():
        return True
    m = _re.search(r"(\d+) failed", res)
    return (not m) or int(m.group(1)) == 0


_bad_list = [lab for lab, res in results if _bad(res)]
if _bad_list:
    print("\n未抓到：")
    for lab in _bad_list:
        print(f"  - {lab}")
    raise SystemExit(1)
raise SystemExit(0)
