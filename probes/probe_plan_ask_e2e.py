"""端到端探测：plan / ask 两个裁决在三个入口的表现。"""
import os, sys, json, tempfile, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def run_cli(home, args):
    env = dict(os.environ, OMEGAFORGE_HOME=home, OMEGAFORGE_MOCK="1")
    p = subprocess.run([sys.executable, "-m", "omegaforge.cli"] + args,
                       capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    return p.returncode, (p.stdout + p.stderr).strip()

home = tempfile.mkdtemp(prefix="probe_plan_")
with open(os.path.join(home, "permissions.json"), "w") as f:
    json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)

from omegaforge.tools.policy import Policy
sys.path.insert(0, ROOT)

for mode in ("confirm", "plan"):
    h = tempfile.mkdtemp(prefix="probe_%s_" % mode)
    with open(os.path.join(h, "permissions.json"), "w") as f:
        json.dump({"terminal": True, "fs": True, "web_fetch": True}, f)
    Policy(h).set_mode(mode)
    print(f"\n===== mode={mode} =====")
    rc, out = run_cli(h, ["fs", "write", "notes/a.txt", "hello"])
    print(f"  CLI fs write  rc={rc}")
    print("  out:", out[:400].replace("\n", "\n       "))
