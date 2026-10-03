"""校验：逐条撤销技能层修复，确认守卫真的会报错。

判据：撤销后对应测试必须 failed（rc != 0）。若仍是 passed，说明守卫是空转。
"""

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())
import os
import shutil
import subprocess
import sys

P = "omegaforge/skills/manager.py"
BAK = "/data/workspace/LATEST/.revtmp/manager.py.bak"
os.makedirs(os.path.dirname(BAK), exist_ok=True)

REVERTS = [
    ("原子升级（回退为先删后拷）", [
        ("        dest = os.path.join(self.root, name)\n"
         "        os.makedirs(self.root, exist_ok=True)\n",
         "        dest = os.path.join(self.root, name)\n"
         "        if os.path.exists(dest):\n"
         "            shutil.rmtree(dest)\n"
         "        os.makedirs(os.path.dirname(dest), exist_ok=True)\n"
         "        shutil.copytree(source_dir, dest)\n"
         "        return {\"installed\": name, \"version\": meta.get(\"version\", \"0\"),\n"
         "                \"description\": meta[\"description\"]}\n"),
    ]),
    ("索引白名单（回退为整份 frontmatter）", [
        ("idx = {k: meta.get(k) for k in _INDEX_KEYS if meta.get(k)}",
         "idx = dict(meta)"),
    ]),
    ("体积上限（去掉长度校验）", [
        ("if len(raw or \"\") > _SKILL_FM_MAX + _SKILL_BODY_MAX:", "if False:"),
        ("if len(body) > _SKILL_BODY_MAX:", "if False:"),
    ]),
    ("scope 强制（去掉权限校验）", [
        ("        if scopes:\n", "        if False:\n"),
    ]),
]

orig = open(P, encoding="utf-8").read()
shutil.copyfile(P, BAK)
results = []

for label, pairs in REVERTS:
    txt = orig
    for a, b in pairs:
        if a not in txt:
            print(f"[SKIP] {label}: 未找到锚点 {a[:40]!r}")
            txt = None
            break
        txt = txt.replace(a, b, 1)
    if txt is None:
        results.append((label, None))
        continue
    open(P, "w", encoding="utf-8").write(txt)
    env = dict(os.environ, PYTHONPATH="/data/workspace/.pylib")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_skill_contract.py",
         "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, env=env, timeout=600)
    tail = [l for l in r.stdout.strip().splitlines() if "passed" in l or "failed" in l]
    nfail = 0
    for l in tail:
        for tok in l.replace(",", " ").split():
            if tok.isdigit():
                pass
    summary = tail[-1] if tail else r.stdout[-200:]
    # 解析失败数
    for l in reversed(r.stdout.strip().splitlines()):
        if "failed" in l:
            try:
                nfail = int(l.split("failed")[0].strip().split()[-1])
            except Exception:
                nfail = 1
            break
    caught = (r.returncode != 0)
    results.append((label, caught, nfail, summary))
    print(f"[REVERT] {label} -> rc={r.returncode} | {summary}")
    shutil.copyfile(BAK, P)

print("\n=== 反向验证汇总 ===")
for item in results:
    label = item[0]
    if item[1] is None:
        print(f"  ? {label}: 锚点缺失，未验证")
    elif item[1]:
        print(f"  OK {label}: 抓到（{item[2]} 项失败）")
    else:
        print(f"  XX {label}: 未抓到 —— 守卫是摆设")

# 确认已还原
now = open(P, encoding="utf-8").read()
print("\n还原一致:", now == orig)

# 退出码必须反映结论。没有出口的脚本恒返回 0，于是"锚点缺失、校验点没
# 执行"与"四个点全抓到"在退出码上无法区分——接入自动流程后它会一直绿着。
_weak = [it[0] for it in results if it[1] is not True]
if now != orig:
    print("还原不一致：源码停在注入态")
    raise SystemExit(1)
if _weak:
    print("\n未抓到：")
    for lab in _weak:
        print(f"  - {lab}")
    raise SystemExit(1)
raise SystemExit(0)
