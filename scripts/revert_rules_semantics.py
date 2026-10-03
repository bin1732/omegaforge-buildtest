"""rules.py 语义修复的校验（相关部分）。

每个校验点撤回一处修复，跑 tests/test_rules_contract.py，必须变红。
撤掉后仍全绿 = 该守卫是空转，或落点找错了。

注入后强制 ast.parse 校验：曾因替换产生 SyntaxError 导致"测试根本没跑起来"
被误读成"抓到了"，假结果比没抓到更危险。
"""

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "omegaforge", "tools", "rules.py")
TEST = os.path.join(ROOT, "tests", "test_rules_contract.py")

ANCHORS = {
    # A. 撤掉 session 作用域隔离（session 规则跨会话仍生效）
    "A": ("""    if r.get("scope") == "session":
        sid = r.get("session_id")
        if sid and sid != session_id:
            return True                  # 上一会话的 session 规则
""",
          """    if False:
        pass
"""),
    # B. 撤掉时间戳容错（脏 expiry 拖垮 _load）
    "B": ("    exp = _coerce_ts(r.get(\"expiry\", 0))",
          "    exp = r.get(\"expiry\", 0)"),
    # B2. 撤掉 _load 内逐条 try（与 B 互相掩盖，单独撤抓不到）
    "B2": ("""            try:
                gone = _rule_expired(r, now, self.session_id)
            except Exception:
                gone = True
""",
           """            gone = _rule_expired(r, now, self.session_id)
"""),
    # C. purge 的 try 保护：必须与 B+B2 组合（单独撤时 _load 仍容错）
    "C": ("""        try:
            n = len(self._load(include_expired=True))
        except Exception:
            n = 0
""",
          """        n = len(self._load(include_expired=True))
"""),
    # D. 撤掉 revoke 取全集（脏规则够不着，只能全清）
    "D": ("        rules = self._load(include_expired=True)",
          "        rules = self._load()"),
}


def run_pytest() -> tuple[int, int]:
    """返回 (failed, passed)。

    必须校验"真的跑了"：会出现 rc≠0 但一条用例都没执行（参数错误、
    收集崩溃），被误读成"抓到了"。0 passed 一律视为不可采信。
    """
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True)
    out = p.stdout + p.stderr
    failed = passed = errors = 0
    for n, kind in re.findall(r"(\d+) (failed|passed|error)", out):
        if kind == "failed":
            failed += int(n)
        elif kind == "passed":
            passed += int(n)
        else:
            errors += int(n)
    return failed + errors, passed


def apply(original: str, keys: list[str]) -> tuple[str, bool]:
    patched = original
    for k in keys:
        if k not in ANCHORS:
            print(f"  未知锚点 {k}")
            return original, False
        old, new = ANCHORS[k]
        if old not in patched:
            print(f"  [{k}] 锚点未命中源码——落点已漂移")
            return original, False
        patched = patched.replace(old, new, 1)
    return patched, True


# 期望：True=必须抓到；False=单撤时会被另一处接住，故单撤查不出。
# 单撤抓不到就照实记为抓不到，不把这类校验点凑成"全绿"的假证据。
EXPECT = {
    "A": True, "D": True,
    "B": False, "B2": False, "C": False,      # 互相掩盖，见下
    "B+B2": True, "B+B2+C": True,
}


def main() -> int:
    original = open(SRC, encoding="utf-8").read()
    # 参数支持 "B+B2" 形式：多校验点同时撤（互相掩盖时必须组合）
    groups = sys.argv[1:] or list(EXPECT)
    ok = True
    try:
        for g in groups:
            keys = g.split("+")
            patched, good = apply(original, keys)
            if not good:
                ok = False
                continue
            try:
                ast.parse(patched)
            except SyntaxError as e:
                print(f"[{g}] 注入后语法错误 {e}——结果不可信")
                ok = False
                continue
            open(SRC, "w", encoding="utf-8").write(patched)
            try:
                failed, passed = run_pytest()
            finally:
                open(SRC, "w", encoding="utf-8").write(original)
            want = EXPECT.get(g, True)
            if passed == 0:
                print(f"[{g}] 不可采信：0 passed（测试未真正执行）")
                ok = False
            elif failed and want:
                print(f"[{g}] 抓到：{failed} failed / {passed} passed")
            elif failed and not want:
                print(f"[{g}] 意外抓到：{failed} failed —— 期望被掩护，"
                      f"需复核假设")
                ok = False
            elif want:
                print(f"[{g}] 未抓到：{passed} passed —— 守卫是摆设或落点错误")
                ok = False
            else:
                print(f"[{g}] 符合预期未抓到：{passed} passed"
                      f"（被另一处接住，互为掩护）")
    finally:
        if open(SRC, encoding="utf-8").read() != original:
            open(SRC, "w", encoding="utf-8").write(original)
            print("警告：源码已强制还原")
    print("REVERT_OK" if ok else "REVERT_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
