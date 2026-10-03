"""真实 MCP 子进程：个人数据写工具是否真的参考 TOOL_RISK 的分级。"""
import os, sys, json, tempfile, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def mcp_call(home, name, args, rid=1):
    env = dict(os.environ, OMEGAFORGE_HOME=home, OMEGAFORGE_MOCK="1")
    msgs = [
        {"jsonrpc":"2.0","id":0,"method":"initialize","params":{
            "protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"p","version":"1"}}},
        {"jsonrpc":"2.0","id":rid,"method":"tools/call","params":{"name":name,"arguments":args}},
    ]
    payload = "".join(json.dumps(m)+"\n" for m in msgs)
    p = subprocess.run([sys.executable,"-m","omegaforge.cli","mcp"],
                       input=payload, capture_output=True, text=True,
                       cwd=ROOT, env=env, timeout=180)
    out=[]
    for ln in p.stdout.splitlines():
        ln=ln.strip()
        if not ln: continue
        try: out.append(json.loads(ln))
        except Exception: pass
    return out

for mode in ("confirm","full"):
    home = tempfile.mkdtemp(prefix="probe_mcppol_"+mode+"_")
    with open(os.path.join(home,"permissions.json"),"w") as f:
        json.dump({"terminal":True,"fs":True,"web_fetch":True}, f)
    from omegaforge.tools.policy import Policy
    from omegaforge.tools.system_tools import McpScope
    Policy(home).set_mode(mode)
    # 授予 kb_add 作用域
    sc = McpScope(home)
    cur = sc.load()
    cur["kb_add"] = True
    sc.save(cur)
    print(f"\n===== mode={mode}, kb_add 作用域已授予 =====")
    res = mcp_call(home, "kb_add", {"title":"probe","text":"内容"})
    for r in res:
        if r.get("id")==1:
            c = r.get("result",{}).get("content",[{}])
            txt = c[0].get("text","") if c else ""
            print("   kb_add ->", txt[:160])
    from omegaforge.tools.policy import audit_read
    print("   审计:", [(a.get("tool"),a.get("verdict"),a.get("reason")) for a in audit_read(limit=10) if a.get("tool")=="kb_add"])
