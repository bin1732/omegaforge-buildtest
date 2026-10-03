"""校验：逐个撤回 LLM 上游守卫的修复，确认守卫真的抓得住。

四个校验点：
  A 撤掉 client._complete 的守卫接通（改回裸 urlopen）→ 03/04/05/06 应失败
  B 撤掉跳转复查                                      → 05 应失败
  C 撤掉响应封顶                                      → 06 应失败
  D 把守卫改成公网口径（连 127.0.0.1 一起禁）          → 01/02 应失败
"""
import os
import shutil
import subprocess
import sys

ROOT = "/data/workspace/LATEST"
CLIENT = os.path.join(ROOT, "omegaforge/llm/client.py")
GUARD = os.path.join(ROOT, "omegaforge/llm/upstream_guard.py")
TEST = "tests/test_llm_upstream_guard.py"

CLIENT_ANCHOR = "raw = open_upstream(req, timeout=90)"
CLIENT_REVERT = (
    "raw = __import__('urllib.request', fromlist=['x'])"
    ".urlopen(req, timeout=90).read().decode('utf-8', 'replace')")

GUARD_REDIRECT = """class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    \"\"\"跳转目标重新过一遍守卫——否则公网地址 302 到元数据即可绕过。\"\"\"

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        guard_upstream_url(newurl)"""
GUARD_REDIRECT_REVERT = """class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):"""

GUARD_CAP = """    if len(raw) > max_bytes:"""
GUARD_CAP_REVERT = """    if False:"""

GUARD_PRIVATE = """    return bool(a.is_link_local or a.is_unspecified)"""
GUARD_PRIVATE_REVERT = """    return bool(a.is_link_local or a.is_unspecified
                 or a.is_private or a.is_loopback)"""


def run(sel):
    env = dict(os.environ, PYTHONPATH=ROOT)
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "-k", sel],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=300)
    tail = [l for l in r.stdout.strip().splitlines() if l.strip()][-1:]
    return r.returncode, (tail[0] if tail else "?")


def patch(path, old, new):
    s = open(path, encoding="utf-8").read()
    assert old in s, f"锚点未命中: {path} :: {old[:60]}"
    open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))


results = {}
bak_c = CLIENT + ".bak"
bak_g = GUARD + ".bak"
shutil.copy2(CLIENT, bak_c)
shutil.copy2(GUARD, bak_g)
try:
    for label, path, old, new, sel in (
        ("A_撤掉client接线", CLIENT, CLIENT_ANCHOR, CLIENT_REVERT,
         "test_03 or test_04 or test_05 or test_06"),
        ("B_撤掉跳转复查", GUARD, GUARD_REDIRECT, GUARD_REDIRECT_REVERT, "test_05"),
        ("C_撤掉响应封顶", GUARD, GUARD_CAP, GUARD_CAP_REVERT, "test_06"),
        ("D_改成公网口径", GUARD, GUARD_PRIVATE, GUARD_PRIVATE_REVERT,
         "test_01 or test_02"),
    ):
        shutil.copy2(bak_c, CLIENT)
        shutil.copy2(bak_g, GUARD)
        patch(path, old, new)
        rc, tail = run(sel)
        results[label] = ("抓到" if rc != 0 else "未抓到（守卫是摆设）", tail)
finally:
    shutil.copy2(bak_c, CLIENT)
    shutil.copy2(bak_g, GUARD)
    os.remove(bak_c)
    os.remove(bak_g)

print()
for k, v in results.items():
    print(f"{k:>18}  {v[0]:<22} {v[1]}")
print()
print("复原后确认:", run("test_01 or test_03")[1])
