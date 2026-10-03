#!/usr/bin/env python3
"""增量推送：只上传与远端不同的文件，其余沿用远端已有的 tree。

全量逐个 POST blob 在本沙盒会触发 502（单次调用里请求过密）。
用法：
  PUSH_REPO=owner/repo [PUSH_REF=refs/heads/main] python3 scripts/push_incremental.py
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = os.environ["GH"]
PRIVATE_REPO = "bin1732/omegaforge"
REPO = os.environ.get("PUSH_REPO", "")
REF = os.environ.get("PUSH_REF", "refs/heads/main")
if not REPO:
    sys.exit("FAIL: 未指定 PUSH_REPO（目标仓库），拒绝按默认值推送")
if REPO == PRIVATE_REPO and os.environ.get("ALLOW_PRIVATE_PUSH") != "1":
    sys.exit(f"FAIL: 目标为私人仓 {PRIVATE_REPO} —— 全部验证通过前禁止覆盖，"
             "确需推送请显式设置 ALLOW_PRIVATE_PUSH=1")
API = f"https://api.github.com/repos/{REPO}"
BRANCH = REF.replace("refs/heads/", "")


def api(method, url, payload=None, retries=5):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API + url, data=data, method=method,
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json",
                         "User-Agent": "OmegaForge-Uploader"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {e.code} {e.read().decode()[:200]}")
        except OSError as e:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {type(e).__name__}: {e}")


def git(*args):
    return subprocess.run(["git", *args], cwd=str(ROOT),
                          capture_output=True, text=True).stdout.strip()


def main():
    head = api("GET", f"/git/ref/heads/{BRANCH}")
    remote_sha = head["object"]["sha"]
    print("remote head:", remote_sha[:12])

    tracked = [l for l in git("ls-files").split("\n") if l.strip()]
    # 远端文件 -> blob sha
    remote_tree = api("GET", f"/git/trees/{remote_sha}?recursive=1")
    remote_blobs = {t["path"]: t["sha"] for t in remote_tree.get("tree", [])
                    if t["type"] == "blob"}
    print(f"本地受版本控制 {len(tracked)} 个，远端 {len(remote_blobs)} 个")

    # 本地 blob sha 用 git 自己算，避免换行符差异导致误判（Windows autocrlf）
    local = {}
    out = git("ls-files", "-s")
    for line in out.split("\n"):
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        local[path] = meta.split()[1]

    to_upload = [p for p in tracked
                 if remote_blobs.get(p) != local.get(p)]
    print(f"需上传 {len(to_upload)} 个")

    tree = []
    for i, rel in enumerate(to_upload):
        p = ROOT / rel
        if not p.exists():
            print("  跳过（已删除但仍在索引中）:", rel)
            continue
        content = base64.b64encode(p.read_bytes()).decode()
        blob = api("POST", "/git/blobs",
                   {"content": content, "encoding": "base64"})
        tree.append({"path": rel, "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
        if (i + 1) % 20 == 0 or i == len(to_upload) - 1:
            print(f"  blobs {i+1}/{len(to_upload)}")

    gone = [p for p in remote_blobs if p not in local]
    for p in gone:
        tree.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
    if gone:
        print(f"  远端多出 {len(gone)} 个（将删除）")

    if not tree:
        print("无变化，未创建新提交")
        return 0

    tr = api("POST", "/git/trees",
             {"base_tree": remote_sha, "tree": tree})
    msg = git("log", "-1", "--pretty=%s")
    cm = api("POST", "/git/commits",
             {"message": msg or "sync", "tree": tr["sha"],
              "parents": [remote_sha]})
    print("commit:", cm["sha"][:10])
    api("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": cm["sha"]})
    print(f"OK 已推送至 {REPO} {BRANCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
