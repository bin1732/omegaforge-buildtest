#!/usr/bin/env python3
"""通过 GitHub Git Data API 推送仓库（绕过 git 协议端口）。"""
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = os.environ["GH"]
# 目标仓库必须显式给出。写死私人仓意味着一次误调用就会在验证完成之前
# 覆盖交付目标——而覆盖是强制的，没有回退。
PRIVATE_REPO = "bin1732/omegaforge"
REPO = os.environ.get("PUSH_REPO", "")
REF = os.environ.get("PUSH_REF", "refs/heads/main")
if not REPO:
    sys.exit("FAIL: 未指定 PUSH_REPO（目标仓库），拒绝按默认值推送")
if REPO == PRIVATE_REPO and os.environ.get("ALLOW_PRIVATE_PUSH") != "1":
    sys.exit(f"FAIL: 目标为私人仓 {PRIVATE_REPO} —— 全部验证通过前禁止覆盖，"
             "确需推送请显式设置 ALLOW_PRIVATE_PUSH=1")
API = f"https://api.github.com/repos/{REPO}"


def api(method: str, url: str, payload: dict | None = None,
        retries: int = 4) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API + url, data=data, method=method,
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json",
                         "User-Agent": "OmniForge-Uploader"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            body = e.read().decode()[:200]
            raise RuntimeError(f"{method} {url} → {e.code} {body}")
        except OSError as e:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} → {type(e).__name__}: {e}")


def main() -> int:
    files = [l for l in subprocess_files()]
    print(f"待上传 {len(files)} 个文件")
    tree = []
    for i, rel in enumerate(files):
        p = ROOT / rel
        content = base64.b64encode(p.read_bytes()).decode()
        blob = api("POST", "/git/blobs",
                   {"content": content, "encoding": "base64"})
        tree.append({"path": rel.replace(os.sep, "/"),
                     "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
        if (i + 1) % 20 == 0 or i == len(files) - 1:
            print(f"  blobs {i+1}/{len(files)}")
    tr = api("POST", "/git/trees", {"tree": tree})
    print("tree:", tr["sha"][:10])
    cm = api("POST", "/git/commits", {
        "message": _commit_message(),
        "tree": tr["sha"]})
    print("commit:", cm["sha"][:10])
    try:
        api("POST", "/git/refs", {"ref": REF, "sha": cm["sha"]})
    except RuntimeError:
        api("PATCH", "/git/refs/" + REF.replace("refs/", ""),
            {"sha": cm["sha"], "force": True})
    for tag in ("v0.1.0", "v0.2.0"):
        try:
            api("POST", "/git/refs",
                {"ref": f"refs/tags/{tag}", "sha": cm["sha"]})
            print("tag:", tag)
        except RuntimeError:
            print("tag exists:", tag)
    print(f"✅ 代码已全部上传至 {REPO} ({REF})")
    return 0


def _commit_message() -> str:
    """提交说明取自本地 HEAD。

    写死一句老话会让远端每条提交都记着同一句，事后无从分辨这批改了什么。
    """
    import subprocess
    r = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(ROOT),
                       capture_output=True, text=True)
    msg = r.stdout.strip()
    return msg or "sync"


def subprocess_files() -> list[str]:
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                         capture_output=True, text=True).stdout
    return [l for l in out.split("\n") if l.strip()]


if __name__ == "__main__":
    sys.exit(main())
