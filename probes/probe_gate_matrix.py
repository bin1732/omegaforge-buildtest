"""探测：工具层风险分级 × 四级模式的裁决对应关系，是否与宣称一致。"""
import os, sys, json, tempfile, inspect
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="probe_gate_")

from omegaforge.tools import system_tools as st
from omegaforge.tools.policy import Policy, TOOL_RISK, POLICY_META

home = os.environ["OMEGAFORGE_HOME"]
with open(os.path.join(home, "permissions.json"), "w") as f:
    json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)

print("=== 1. POLICY_META 宣称的工具 ===")
meta = POLICY_META()
print("   tool_risk 条目数:", len(meta["tool_risk"]))
print("   TOOL_RISK 条目数:", len(TOOL_RISK))
print("   一致:", set(meta["tool_risk"]) == set(TOOL_RISK))

print("\n=== 2. 系统工具实际调用门禁的方法 ===")
for name, fn in inspect.getmembers(st.SystemTools, inspect.isfunction):
    if name.startswith("_") and name != "_gate":
        continue
    src = inspect.getsource(fn)
    if "_gate(" in src:
        import re
        m = re.findall(r'self\._gate\("([^"]+)"', src)
        print(f"   {name:16s} -> {m}")
    elif name in ("fs_read","fs_write","fs_list","run_command","web_fetch"):
        print(f"   {name:16s} -> !! 没有调用 _gate")

print("\n=== 3. full 模式下各工具的实际裁决 ===")
Policy(home).set_mode("full")
t = st.SystemTools(home)
for tool in list(TOOL_RISK):
    d = t.policy.decide(tool)
    print(f"   {tool:16s} risk={d['risk']:6s} cap={d['capability']:9s} -> {d['verdict']}")

print("\n=== 4. confirm 模式下 ===")
Policy(home).set_mode("confirm")
t2 = st.SystemTools(home)
for tool in ["fs.read", "fs.write", "terminal", "kb_add", "task_add", "web_fetch"]:
    d = t2.policy.decide(tool)
    print(f"   {tool:16s} risk={d['risk']:6s} -> {d['verdict']}")

print("\n=== 5. 未登记工具 ===")
d = t2.policy.decide("totally_unknown_tool")
print("   ", d)
