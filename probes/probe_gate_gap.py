"""探测：写用户数据的真实入口，是否都过门禁。重点看 delete 类与 MCP 直连。"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="probe_gap_")
from omegaforge.tools.policy import TOOL_RISK

home = os.environ["OMEGAFORGE_HOME"]
with open(os.path.join(home, "permissions.json"), "w") as f:
    json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)

print("=== 1. TOOL_RISK 里登记的工具，在代码里是否真实存在 ===")
import subprocess
names = list(TOOL_RISK)
for n in names:
    hits = subprocess.run(["grep","-rl",n,"omegaforge/","--include=*.py"],
                          capture_output=True, text=True).stdout.split()
    if not hits:
        print(f"   {n:16s} -> !! 代码里没有任何引用（登记了不存在的工具）")

print("\n=== 2. 删除类工具是否真实存在 ===")
for n in ["kb_delete","task_delete","wiki_delete","memory_delete","memory_forget"]:
    hits = subprocess.run(["grep","-rn","def "+n,"omegaforge/","--include=*.py"],
                          capture_output=True, text=True).stdout.strip()
    print(f"   {n:16s} -> {hits[:110] if hits else '!! 无定义'}")

print("\n=== 3. MCP server 里写工具是否过门禁 ===")
for n in ["kb_add","memory_remember","wiki_save","task_add"]:
    hits = subprocess.run(["grep","-rn",n+r"\|tool_dispatch\|self\.kb","omegaforge/mcp_server.py"],
                          capture_output=True, text=True).stdout.strip()
    print(f"   --- {n} ---")
    for ln in hits.splitlines()[:6]:
        print("     ", ln[:120])
