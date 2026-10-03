"""任务列表覆盖范围守卫。

为什么需要它：

后端 /api/tasks/list 的 scope 默认是 pending，只返回未完成的条目。
界面若照默认取，"显示已完成"这个开关就没有任何东西可显示——
用户点完成，条目从列表消失；再点"显示已完成"，它仍然不出现，
而概览里明明写着"已完成 1"。用户在界面上完成一件事之后，
就再也看不到它了。

当时所有接口级验收都是绿的：列表接口有数据、统计字段正确、
完成接口返回成功。只有站在界面上真的点一遍才会发现。

两类失效都要挡：
  1. 界面用默认 scope（拿不到已完成的条目）
  2. 界面拿到全量后，却在客户端把已完成的过滤掉（开关形同虚设）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "frontend" / "src" / "lib" / "api.ts"
PAGE = ROOT / "frontend" / "src" / "pages" / "TasksPage.tsx"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def check() -> list[str]:
    """返回问题清单；空列表表示没问题。"""
    bad: list[str] = []
    api = _read(API)
    page = _read(PAGE)

    if not api:
        # 读不到就不能判通过：文件被移动/改名时，下面的所有匹配都会落空，
        # 于是守卫"通过"而实际上什么都没查。
        return ["读不到 api.ts，无法判定"]
    if not page:
        return ["读不到 TasksPage.tsx，无法判定"]

    # 1. 取列表必须是全量
    m = re.search(r"tasksList\s*=\s*\([^)]*\)\s*=>\s*(.*)", api, re.S)
    if not m:
        return ["找不到 tasksList 定义"]
    body = m.group(1)
    if "scope=" not in body:
        bad.append("tasksList 未指定 scope：后端默认为 pending，"
                   "界面将永远拿不到已完成的条目")
    elif "all" not in body:
        bad.append("tasksList 的 scope 不是 all：已完成的条目仍会缺失")

    # 2. 客户端不得在渲染前把已完成的过滤掉，否则开关形同虚设
    # 只看 visible 这一行：它是唯一决定列表渲染什么的地方。
    vm = re.search(r"const\s+visible\s*=\s*(.+)", page)
    if not vm:
        return ["TasksPage 找不到 visible 定义，无法判定"]
    expr = vm.group(1)
    # showDone 为真时必须显示全量；写成 items.filter(!done) 会永远过滤掉
    if "showDone" not in expr:
        bad.append("visible 未依据 showDone 决定：开关不生效")
    elif re.search(r"items\.filter\([^)]*!t\.done", expr) and "?" not in expr:
        bad.append("visible 无条件过滤已完成：开关形同虚设")

    return bad


def main() -> int:
    bad = check()
    if bad:
        print("FAIL 任务列表覆盖范围：")
        for b in bad:
            print("  -", b)
        return 1
    print("OK 任务列表覆盖全部条目，已完成在界面上可见")
    return 0


if __name__ == "__main__":
    sys.exit(main())
