#!/usr/bin/env python3
"""数据目录环境变量名守卫：设了错名字不报错，只是完全不生效。

## 为什么必须有这一条

`resolve_home()` 读的环境变量名写在 `core/paths.py` 的 `_ENV` 里。任何
给子进程设数据目录的地方，用的必须是**那一个名字**。写错名字不会抛异常、
不会打日志，只是安静地不生效——数据仍落到 cwd。

后果分两种，都不指向环境变量：

 * 以为隔离了而没隔离 —— 验收写进安装目录，界面上看到的却是别处的数据，
   症状表现为"刚写的东西读不回来"
 * 以为落在用户目录而落在 cwd —— 打包后 cwd 是安装目录，数据被写进一个
   不在卸载清单里的位置，卸载后成为无人知晓的孤儿

## 判定

从 `paths.py` 读出 `_ENV` 的真实值，再扫所有设置点。凡出现以 `_HOME`
结尾且不等于该值的名字，一律失败——包括历史上写过的 `OF_HOME`。

口径必须是"读 paths.py"，不能把名字写死在守卫里：写死后 paths.py 一旦
改名，所有设置点同时失效而守卫照绿。

用法：python3 scripts/check_home_env_name.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATHS = ROOT / "omegaforge" / "core" / "paths.py"
SCAN_DIRS = [ROOT / "scripts", ROOT / "probes", ROOT / "tests",
             ROOT / "omegaforge"]
# 匹配 `env["X_HOME"] = `、`os.environ["X_HOME"]`、`X_HOME=` 三种写法
SET_RE = re.compile(r'["\']([A-Za-z_]*_HOME)["\']\s*\]?\s*=')
ENVIRON_RE = re.compile(r'environ(?:\.get)?\s*\(\s*["\']([A-Za-z_]*_HOME)["\']')


def read_env_name() -> str:
    """从 paths.py 读出真实的环境变量名，读不到则失败。

    读不到时必须判失败而不是退回一个猜测值：猜测值与真实值不同时，
    守卫会在每一个设置点上误判，于是整段形同没有。
    """
    if not PATHS.is_file():
        raise SystemExit(f"FAIL: 找不到 {PATHS}，无法确认环境变量名")
    tree = ast.parse(PATHS.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_ENV":
                    if isinstance(node.value, ast.Constant):
                        return str(node.value.value)
    raise SystemExit("FAIL: paths.py 里读不到 _ENV —— 不得用猜测值继续")


# 与数据目录无关、但同样以 _HOME 结尾的环境变量。必须登记理由：
# 不登记就放过的话，任何新写的错名字都能自称"是别的东西"蒙混过去。
NON_DATA_HOME = {
    "FE_HOME": "前端构建环境目录，与 omegaforge 数据目录无关",
}


def scan(env_name: str) -> list[str]:
    bad = []
    self_path = Path(__file__).resolve()
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            # 本文件里有正则字面量 X_HOME，扫自己必然误报。误报的代价是
            # 守卫在正确实现上变红，而人倾向于把整条删掉。
            if p.resolve() == self_path:
                continue
            try:
                s = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in list(SET_RE.finditer(s)) + list(ENVIRON_RE.finditer(s)):
                if m.group(1) == env_name:
                    continue
                if m.group(1) in NON_DATA_HOME:
                    continue
                line = s[:m.start()].count("\n") + 1
                bad.append(f"{p.relative_to(ROOT)}:{line}  "
                           f"{m.group(1)}（应为 {env_name}）")
    return bad


def main() -> int:
    env_name = read_env_name()
    bad = scan(env_name)
    if bad:
        print(f"FAIL: 数据目录环境变量名与 paths.py 的 _ENV 不一致"
              f"（应为 {env_name}）：")
        for b in bad:
            print("  ❌ " + b)
        print("  写错名字不报错，只是完全不生效：数据仍落到 cwd。")
        return 1
    print(f"数据目录环境变量名守卫通过 ✓（{env_name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
