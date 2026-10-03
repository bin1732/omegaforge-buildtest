"""基线：MCP 上 fs_write 在 confirm 模式下是什么表现（与个人写工具对照）。"""
import os, sys, json, tempfile, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def mcp_call(home, name, args, rid=1):
    env = dict(os.environ, OMEGAFORGE_HOME=home, OMEGAFORGE_MOCK="1")
    msgs=[{"jsonrpc":"2.0","id":0,"method":"initialize","params":{
        "protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"p","version":"1"}}},
        {"jsonrpc":"2.0","id":rid,"method":"tools/call","params":{"name":name,"arguments":args}}]
    p=subprocess.run([sys.executable,"-m","omegaforge.cli","mcp"],
        input="".join(json.dumps(m)+"\n" for m in msgs),
        capture_output=True,text=True,cwd=ROOT,env=env,timeout=180)
    out=[]
    for ln in p.stdout.splitlines():
        ln=ln.strip()
        if ln:
            try: out.append(json.loads(ln))
            except Exception: pass
    return out

for mode in ("confirm","auto_edit","plan"):
    home=tempfile.mkdtemp(prefix="probe_fs_"+mode+"_")
    with open(os.path.join(home,"permissions.json"),"w") as f:
        json.dump({"terminal":True,"fs":True,"web_fetch":True}, f)
    from omegaforge.tools.policy import Policy
    from omegaforge.tools.system_tools import McpScope
    Policy(home).set_mode(mode)
    sc=McpScope(home); cur=sc.load(); cur["fs.write"]=True; sc.save(cur)
    res=mcp_call(home,"fs_write",{"path":"a.txt","content":"hello"})
    txt=""
    for r in res:
        if r.get("id")==1:
            c=r.get("result",{}).get("content",[{}])
            txt=(c[0].get("text","") if c else "")[:120]
            if r.get("result",{}).get("isError"): txt="[isError] "+txt
    print(f"  mode={mode:10s} fs_write -> {txt}")
    from omegaforge.tools.policy import audit_read
    print("     审计:",[(a.get("tool"),a.get("verdict")) for a in audit_read(limit=20) if a.get("tool")=="fs.write"])
