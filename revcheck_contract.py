#!/usr/bin/env python3
"""契约守卫校验：逐项撤回修复，确认守卫真的会红。

每条校验点：先断言原文命中（避免"没撤到却报抓到"的假绿），
再跑守卫，最后无论成败都还原。
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.join(ROOT, "omegaforge", "server.py")
API = os.path.join(ROOT, "frontend", "src", "lib", "api.ts")
GUARD = os.path.join(ROOT, "tests", "test_frontend_contract_guard.py")

BAK_SRV = SRV + ".revbak"
BAK_API = API + ".revbak"

ANCHORS = [
    ("会话接口只认 id（撤回 conversation_id 别名）", SRV,
     'CONVS.delete(pick_first(payload,\n                                                    ("conversation_id", "id")))',
     'CONVS.delete(str(payload.get("id", "")))'),
    ("/api/runs 改成裸数组（撤回包装对象）", SRV,
     'self._json({"runs": RUNS.list()})',
     'self._json(RUNS.list())'),
    ("会话 model 只认 id（撤回 conversation_id 别名）", SRV,
     'cid = pick_first(payload, ("conversation_id", "id"))',
     'cid = str(payload.get("id", ""))'),
    ("前端 fetchRuns 改回裸数组假设", API,
     "export const fetchRuns = async (): Promise<RunItem[]> => {\n  const d = await get<{ runs?: RunItem[] }>('/api/runs')\n  return Array.isArray(d?.runs) ? d.runs : []\n}",
     "export const fetchRuns = () => get<RunItem[]>('/api/runs')"),
]


def run_guard():
    p = subprocess.run([sys.executable, GUARD], cwd=ROOT,
                       capture_output=True, text=True, timeout=180)
    out = p.stdout
    nfail = out.count("[FAIL]")
    total = out.count("[PASS]") + nfail
    names = [l.split("] ", 1)[1].split(" —")[0]
             for l in out.splitlines() if "[FAIL]" in l]
    return p.returncode, total, nfail, names


def main():
    shutil.copy2(SRV, BAK_SRV)
    shutil.copy2(API, BAK_API)
    results = []
    try:
        for label, path, old, new in ANCHORS:
            with open(path, encoding="utf-8") as f:
                src = f.read()
            if old not in src:
                results.append((label, "锚点未命中（脚本与源码不同步）", False))
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(src.replace(old, new, 1))
            try:
                rc, total, nfail, names = run_guard()
                ok = rc != 0 and nfail > 0
                results.append((label, f"{nfail}/{total} 失败：{names[:4]}", ok))
            finally:
                shutil.copy2(BAK_SRV, SRV)
                shutil.copy2(BAK_API, API)
    finally:
        shutil.copy2(BAK_SRV, SRV)
        shutil.copy2(BAK_API, API)
        os.remove(BAK_SRV)
        os.remove(BAK_API)

    print("\n=== 反向验证 ===")
    allok = True
    for label, detail, ok in results:
        print(f"  [{'抓到' if ok else '未抓到'}] {label} — {detail}")
        allok = allok and ok
    # 还原后守卫必须全绿
    rc, total, nfail, _ = run_guard()
    print(f"\n还原后守卫：{total - nfail}/{total} 通过")
    return 0 if (allok and nfail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
