#!/usr/bin/env python3
"""后端候选端口与界面候选端口必须一致。

后端在端口被占用时会退到候选端口里的下一个；界面按同一组端口做发现。
两边一旦不一致，表现是"后端在跑、界面一律报连不上"——用户只会看到
"无法连接到本机服务"，而排查方向会被带去服务启动环节。

判定口径：
* 后端取 ``PORT_CANDIDATES`` 的字面量元组（AST，注释里的同名文字不算）；
* 界面取 ``PORT_CANDIDATES`` 的数组字面量；
* 任一侧缺失或为空即判失败——读不到内容时若返回"通过"，本守卫
  会退化成恒真，路径写错或文件改名都查不出来。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

BACKEND_REL = "omegaforge/server.py"
FRONTEND_REL = "frontend/src/lib/api.ts"


def backend_candidates(root: Path) -> tuple[list[int] | None, str | None]:
    path = root / BACKEND_REL
    if not path.is_file():
        return None, f"后端文件不存在：{path}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return None, f"后端文件无法解析：{path}: {exc}"
    for node in tree.body:
        name = None
        value = None
        # 带类型注解的常量是 AnnAssign（无 targets），不带注解的是 Assign。
        # 只认其中一种会让另一侧的定义"查不到"，而查不到若不判失败就会
        # 退化成恒真。
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name, value = target.id, node.value
                    break
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        if name != "PORT_CANDIDATES":
            continue
        if isinstance(value, (ast.Tuple, ast.List)):
            out: list[int] = []
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, int):
                    out.append(elt.value)
                else:
                    return None, f"候选端口含非整数字面量：{path}:{elt.lineno}"
            return out, None
        return None, f"PORT_CANDIDATES 不是字面量序列：{path}:{node.lineno}"
    return None, f"后端未定义 PORT_CANDIDATES：{path}"


def frontend_candidates(root: Path) -> tuple[list[int] | None, str | None]:
    path = root / FRONTEND_REL
    if not path.is_file():
        return None, f"界面文件不存在：{path}"
    text = path.read_text(encoding="utf-8")
    # 先去掉行注释再取数组字面量：注释里出现的同形文字不参与判定，否则
    # 一条正在说明本规则的行内说明会被当成定义，守卫会在正确实现上变红。
    # 仅去掉前面不是冒号的 //，以免把 http://127.0.0.1 这类地址截断。
    text = re.sub(r"(?<!:)//[^\n]*", "", text)
    m = re.search(
        r"const\s+PORT_CANDIDATES\s*(?::[^=\n]+)?=\s*\[([^\]]*)\]", text
    )
    if not m:
        return None, f"界面未定义 PORT_CANDIDATES 数组：{path}"
    body = m.group(1)
    nums = re.findall(r"\d+", body)
    if not nums:
        return None, f"界面候选端口为空：{path}"
    return [int(n) for n in nums], None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验后端与界面的候选端口一致")
    ap.add_argument("--root", default=".", help="仓库根目录")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    bad: list[str] = []
    be, err = backend_candidates(root)
    if err:
        bad.append(err)
    fe, err2 = frontend_candidates(root)
    if err2:
        bad.append(err2)

    if be and fe and be != fe:
        bad.append(f"候选端口不一致：后端 {be} / 界面 {fe}")

    if bad:
        print("FAIL 候选端口守卫")
        for line in bad:
            print("  -", line)
        return 1
    print(f"OK 候选端口一致（{len(be or [])} 个）：{be}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
