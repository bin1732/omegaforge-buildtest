#!/usr/bin/env python3
"""门禁矩阵修复的校验：撤掉修复，守卫必须变红。"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEST = "tests/test_gate_matrix_contract.py"

FILES = {
    "pol": ROOT / "omegaforge" / "tools" / "policy.py",
    "mcp": ROOT / "omegaforge" / "mcp_server.py",
    "err": ROOT / "omegaforge" / "core" / "errors.py",
}
ORIG = {k: p.read_text(encoding="utf-8") for k, p in FILES.items()}

ANCHORS = [
    ("A MCP 个人写工具接矩阵（撤掉 gate_personal_write 调用）",
     [("mcp", '            gate_personal_write(name, args, origin="mcp")\n', "")],
     "McpPersonalWriteTest"),

    ("B 放行侧审计（allow 不写审计）",
     [("pol", '    if verdict == "allow":\n'
              '        audit_write(dict(ev, verdict="allow", '
              'reason="personal_write"))\n',
       '    if verdict == "allow":\n')],
     "McpPersonalWriteTest"),

    ("C PlanRequired 的错误分类分支",
     [("err", '    if name == "PlanRequired":\n', '    if False:\n')],
     "PlanModeTest"),

    ("D TOOL_RISK 补 wiki_save / wiki_search",
     [("pol", '    "wiki_save":       ("", "medium"),\n', ""),
      ("pol", '    "wiki_search":     ("", "low"),\n', "")],
     "NameRegistrationTest"),

    ("E MCP 上 ask 的可行动指引",
     [("err", '        if getattr(exc, "origin", None) == "mcp":\n',
       '        if False:\n')],
     "McpApprovalGuidanceTest"),
]


def run(kls: str) -> tuple:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-k", kls, "-q", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    tail = [l for l in p.stdout.strip().splitlines() if l.strip()]
    return p.returncode, (tail[-1] if tail else "")


def main() -> int:
    caught = 0
    for name, subs, kls in ANCHORS:
        cur = dict(ORIG)
        missing = False
        for key, old, new in subs:
            if old not in cur[key]:
                print(f"  !! 锚点 {name} 未命中锚串（守卫可能没覆盖）")
                missing = True
                break
            cur[key] = cur[key].replace(old, new, 1)
        if missing:
            continue
        for k, p in FILES.items():
            p.write_text(cur[k], encoding="utf-8")
        try:
            rc, tail = run(kls)
        finally:
            for k, p in FILES.items():
                p.write_text(ORIG[k], encoding="utf-8")
        red = rc != 0
        caught += int(red)
        print(f"  [{'抓到' if red else '未抓到'}] {name}  -> {tail}")

    print(f"\n反向验证：{caught}/{len(ANCHORS)} 抓到")
    for k, p in FILES.items():
        assert p.read_text(encoding="utf-8") == ORIG[k], f"{p} 未复原！"
    print("源码已复原")
    return 0 if caught == len(ANCHORS) else 1


if __name__ == "__main__":
    sys.exit(main())
