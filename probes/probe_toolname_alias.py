"""探测：TOOL_RISK 登记的名字 与 实际暴露的工具名是否对得上。"""
import os, sys, tempfile, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="probe_alias_")

from omegaforge.tools.policy import TOOL_RISK

# MCP server 暴露的工具名
src = open("omegaforge/mcp_server.py", encoding="utf-8").read()
mcp_names = set(re.findall(r'"name":\s*"([a-z_]+)"', src))
mcp_names |= set(re.findall(r'^\s{8}"([a-z_]+)":\s*\{', src, re.M))
print("=== MCP 暴露的工具名 ===")
print(sorted(mcp_names))

print("\n=== TOOL_RISK 登记的名字 ===")
print(sorted(TOOL_RISK))

print("\n=== 实际存在但【未登记】的工具（会落进 unknown→high）===")
# 真实可调用的个人工具
real = set()
for root, _, files in os.walk("omegaforge"):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            txt = open(p, encoding="utf-8").read()
            for m in re.finditer(r"def (kb_\w+|task_\w+|wiki_\w+|memory_\w+)\(", txt):
                real.add(m.group(1))
print("   代码中定义的个人工具:", sorted(real))
for n in sorted(real - set(TOOL_RISK)):
    print(f"   !! {n:20s} 已定义但未登记 -> decide() 按 ('', 'high') 处理")

print("\n=== 登记了但代码里不存在 ===")
for n in sorted(set(TOOL_RISK) - real):
    if n.startswith(("kb_","task_","wiki_","memory_")):
        print(f"   !! {n:20s} 登记但无定义")
