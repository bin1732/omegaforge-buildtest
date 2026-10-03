#!/usr/bin/env python3
"""查询测试仓 CI 构建状态与步骤结论。

git 协议在本沙盒被 403，只能用 Git Data / Actions API。
用法：
  python3 probes/ci_status.py                 最近 5 轮一句话状态
  python3 probes/ci_status.py --run <id>      该轮每个步骤的结论
  python3 probes/ci_status.py --log <id> <步骤名片段>   该步骤日志（片段匹配）
"""
import json
import os
import sys
import urllib.request
import zipfile
import io

REPO = os.environ.get("CI_REPO", "bin1732/omegaforge-buildtest")
TOKEN = os.environ.get("GH", "")
if not TOKEN:
    sys.exit("FAIL: 未设置 GH 令牌")

API = f"https://api.github.com/repos/{REPO}"


def api(url, raw=False):
    req = urllib.request.Request(
        API + url if url.startswith("/") else url,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "OmegaForge-CI"})
    with urllib.request.urlopen(req, timeout=90) as r:
        body = r.read()
    return body if raw else json.loads(body.decode() or "{}")


def runs(n=5):
    d = api(f"/actions/runs?per_page={n}")
    return d.get("workflow_runs", [])


def jobs(run_id):
    d = api(f"/actions/runs/{run_id}/jobs?per_page=100")
    return d.get("jobs", [])


def main():
    args = sys.argv[1:]
    if args and args[0] == "--run":
        run_id = args[1]
        for j in jobs(run_id):
            print(f"== job {j['name']}  conclusion={j.get('conclusion')}")
            for s in j.get("steps", []):
                num = str(s.get("number") or "")
                con = str(s.get("conclusion") or "-")
                print(f"  {num:>2} {con:<10} {s.get('name')}")
        return
    if args and args[0] == "--log":
        run_id, frag = args[1], args[2]
        for j in jobs(run_id):
            z = api(j["url"] + "/logs", raw=True)
            with zipfile.ZipFile(io.BytesIO(z)) as zf:
                for name in zf.namelist():
                    if frag.lower() in name.lower():
                        print(f"########## {name}")
                        print(zf.read(name).decode("utf-8", "replace"))
        return
    for r in runs():
        print(f"{r['id']}  {r.get('status'):<10} {str(r.get('conclusion')):<10} "
              f"{r.get('head_sha','')[:12]}  {r.get('display_title','')}")


if __name__ == "__main__":
    main()
