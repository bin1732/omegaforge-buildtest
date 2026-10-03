#!/usr/bin/env python3
"""权限级别变更审计修复的校验。

把修复逐个退回，确认守卫真的会红。判据不是"注入成功"，而是
**有真实的 failed 数**——测试若因语法损坏根本没跑起来，退出码同样非 0，
会把"没跑"误读成"抓到"。因此每次注入后强制 ast.parse 校验语法。

校验点
  A 退回双重静默（except Exception: pass）      → 应抓到
  B audit_write 成功失败都返回 True（不实报）   → 应抓到
  C 审计失败时把权限回滚（过度修复）            → 应抓到（防修过头）
  D 只报"审计失败"不说"已生效"（误导性文案）    → 应抓到
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
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "omegaforge" / "tools" / "policy.py"
GUARD = ROOT / "tests" / "test_policy_audit_integrity.py"

ANCHORS = [
    ("A 退回双重静默", {
        "审计写入失败时_必须让用户知情",
        "审计失败时_权限级别不回滚",
    }, [
        (POLICY,
         '''        ok = audit_write({"tool": "policy.set_mode", "verdict": "allow",
                          "from": old, "to": mode,
                          "risk": "critical" if mode == "full" else "medium"})
        if not ok:''', '''        try:
            audit_write({"tool": "policy.set_mode", "verdict": "allow",
                         "from": old, "to": mode,
                         "risk": "critical" if mode == "full" else "medium"})
        except Exception:
            pass
        if False:'''),
    ]),
    ("B audit_write 不实报成败", {
        # 不实报会连带影响"单次工具执行"那条：它断言失败时返回 False
        "audit_write_如实返回成败",
        "审计写入失败时_必须让用户知情",
        "审计失败时_权限级别不回滚",
        "审计失败不阻断_单次工具执行路径",
    }, [
        (POLICY,
         '''        return True
    except OSError as e:''', '''        return True
    except OSError as e:
        return True  # 注入：不实报
        '''),
    ]),
    ("C 审计失败时回滚权限（过度修复）", {
        "审计失败时_权限级别不回滚",
    }, [
        (POLICY,
         '''        if not ok:
            # 新的权限级别已经落盘生效，此时绝不能返回成功''',
         '''        if not ok:
            atomic_write_json(self.path, {"mode": old})  # 注入：回滚
            # 新的权限级别已经落盘生效，此时绝不能返回成功'''),
    ]),
    ("D 文案只说审计失败不说已生效", {
        "审计写入失败时_必须让用户知情",
    }, [
        (POLICY,
         '''                f"权限级别已更新为「{MODE_LABELS.get(mode, mode)}」，"
                "但审计记录未能写入，请检查数据目录是否可写后重新设置"''',
         '''                "审计记录未能写入，请检查数据目录是否可写后重新设置"'''),
    ]),
]


def inject(pairs):
    for path, old, new in pairs:
        src = path.read_text(encoding="utf-8")
        if old not in src:
            raise SystemExit(f"注入失败：{path.name} 里找不到锚点文本")
        shutil.copy2(path, str(path) + ".bak")
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
        # 语法校验前置：语法坏了的话 failed 数是假的
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            for p, _, _ in pairs:
                shutil.move(str(p) + ".bak", p)
            raise SystemExit(f"注入后语法损坏（{e}），已回滚")


def restore(pairs):
    for path, _, _ in pairs:
        bak = str(path) + ".bak"
        if os.path.exists(bak):
            shutil.move(bak, path)


def run_guard():
    env = dict(os.environ)
    p = subprocess.run(
        [sys.executable, "-m", "pytest", str(GUARD), "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    out = p.stdout + p.stderr
    names = set()
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("FAILED") or "::" in ln and "failed" not in ln:
            pass
    # 解析失败用例名
    for ln in out.splitlines():
        if ln.startswith("FAILED "):
            nm = ln.split(" ", 1)[1].split(" ")[0].split("::")[-1]
            # 用例函数名带 test_ 前缀，期望表里写的是去掉前缀的名字
            if nm.startswith("test_"):
                nm = nm[len("test_"):]
            names.add(nm)
    import re
    m = re.search(r"(\d+) failed", out)
    n = int(m.group(1)) if m else 0
    return n, names, out


def main():
    # 自愈：清掉上一版被中断留下的注入痕迹
    for _, _, pairs in ANCHORS:
        for path, old, new in pairs:
            try:
                src = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            if new in src and old not in src:
                path.write_text(src.replace(new, old, 1), encoding="utf-8")
                print(f"  [自愈] 还原上轮遗留注入：{path.name}")
            bak = str(path) + ".bak"
            if os.path.exists(bak):
                os.remove(bak)

    n0, f0, _ = run_guard()
    print(f"基线：failed={n0} 失败={sorted(f0)}")
    if n0 != 0:
        raise SystemExit("基线就不是全绿，反向验证没有意义")

    allok = True
    for name, expect, pairs in ANCHORS:
        inject(pairs)
        try:
            n, fails, out = run_guard()
        finally:
            restore(pairs)
        got = set(fails)
        want = set(expect)
        ok = n > 0 and want.issubset(got) and not (got - want)
        print(f"{'  [抓到]' if ok else '  [未抓到]'} {name}: failed={n} {sorted(got)}")
        if not ok:
            allok = False
            print(f"      期望={sorted(want)}")
            print("      " + "\n      ".join(out.splitlines()[-6:]))

    n1, f1, _ = run_guard()
    print(f"还原后：failed={n1}")
    if n1 != 0:
        print("  [警告] 还原后仍未全绿，产物可能残留注入")
        allok = False
    print("RESULT:", "ALL_CAUGHT" if allok else "INCOMPLETE")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
