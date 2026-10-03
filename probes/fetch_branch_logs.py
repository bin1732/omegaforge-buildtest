#!/usr/bin/env python3
"""从回传分支取回构建日志（job 日志托管在 Azure Blob，对沙盒 403）。

用法：
  python3 probes/fetch_branch_logs.py <branch> [匹配片段] [--out 目录]
"""
import base64
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ci_status import api  # noqa: E402

REPO = os.environ.get("CI_REPO", "bin1732/omegaforge-buildtest")


def main():
    args = [a for a in sys.argv[1:]]
    out_dir = "."
    if "--out" in args:
        i = args.index("--out")
        out_dir = args[i + 1]
        del args[i:i + 2]
    branch = args[0]
    frag = args[1] if len(args) > 1 else ""
    os.makedirs(out_dir, exist_ok=True)

    ref = api(f"/git/ref/heads/{branch}")
    sha = ref["object"]["sha"]
    tr = api(f"/git/trees/{sha}?recursive=1")
    blobs = [t for t in tr.get("tree", []) if t["type"] == "blob"]
    hit = [b for b in blobs if frag.lower() in b["path"].lower()]
    print(f"分支 {branch} 共 {len(blobs)} 个文件，命中 {len(hit)} 个")
    for b in hit[:20]:
        d = api(f"/git/blobs/{b['sha']}")
        raw = base64.b64decode(d.get("content", "")) if d.get("encoding") == "base64" else b""
        p = os.path.join(out_dir, os.path.basename(b["path"]))
        open(p, "wb").write(raw)
        print(f"  {b['path']} -> {p} ({len(raw)} 字节)")


if __name__ == "__main__":
    main()
