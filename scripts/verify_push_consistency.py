#!/usr/bin/env python3
"""推送后逐条核对：远端每个文件的 blob sha 必须与本地一致。

## 为什么需要它

推送走 Git Data API（沙盒出口对 github.com 主域一律拒绝，git 协议不可用），
逐文件建 blob。任一步失败都可能只上传了一部分，而 API 调用是成功的——
报"已推送"与"全部推到"不是一回事。不看 sha 的话，远端缺文件、或某个文件
内容与本地不同，都要等到装机验收才暴露，而那时表现为"功能坏了"——症状
出现在产品侧，原因却在推送侧。

## 判定

  * 远端每个 blob 的 sha 必须等于本地文件的 git blob sha（同一算法）
  * 本地已跟踪的文件在远端必须都存在——缺一个即失败，不静默跳过
  * 读不到远端树（分支不存在 / 无权限）必须判失败：空结果会被当成"全都
    一致"，那正是此脚本要防的事
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path("/data/workspace/.github_token")


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    sys.exit("FAIL: 读不到令牌（GITHUB_TOKEN 或 /data/workspace/.github_token）")


def api(path: str):
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "omegaforge-push-verify",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"FAIL: API {path} → HTTP {e.code}")


def blob_sha(data: bytes) -> str:
    """git blob sha：sha1("blob <len>\\0" + 内容)，与 GitHub 同一算法。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def local_files() -> list[str]:
    """本地已跟踪文件——与推送收集同一口径。"""
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        sys.exit("FAIL: git ls-files 失败")
    out = []
    for rel in proc.stdout.decode("utf-8", "surrogateescape").split("\0"):
        rel = rel.strip()
        if rel and (ROOT / rel).is_file():
            out.append(rel)
    return sorted(out)


def remote_tree(repo: str, branch: str) -> dict[str, str]:
    """远端某分支的 路径 → blob sha。取不到即失败，绝不能返回空表蒙混。"""
    ref = api(f"/repos/{repo}/git/ref/heads/{branch}")
    tree = api(f"/repos/{repo}/git/trees/{ref['object']['sha']}?recursive=1")
    if tree.get("truncated"):
        sys.exit("FAIL: 远端树被截断（truncated）—— 核对不完整等同于没核对")
    out = {}
    for item in tree.get("tree", []):
        if item.get("type") == "blob":
            out[item["path"]] = item["sha"]
    if not out:
        sys.exit("FAIL: 远端树为空——读不到分支内容时不得判为一致")
    return out


def main() -> int:
    repo = os.environ.get("PUSH_REPO", "").strip()
    if not repo:
        sys.exit("FAIL: 未指定 PUSH_REPO，拒绝按默认值核对")
    branch = os.environ.get("PUSH_BRANCH", "").strip()
    if not branch:
        meta = api(f"/repos/{repo}")
        branch = (meta.get("default_branch") or "").strip() or "main"

    files = local_files()
    if len(files) < 100:
        sys.exit(f"FAIL: 本地只有 {len(files)} 个已跟踪文件，口径疑似失效")
    remote = remote_tree(repo, branch)

    missing = [f for f in files if f not in remote]
    differ = []
    for rel in files:
        sha = remote.get(rel)
        if sha is None:
            continue
        got = blob_sha((ROOT / rel).read_bytes())
        if got != sha:
            differ.append(rel)

    print(f"本地已跟踪 {len(files)} 个文件；远端 {len(remote)} 条 blob")
    if missing:
        print(f"❌ 远端缺失 {len(missing)} 个：")
        for f in missing[:10]:
            print("   ", f)
    if differ:
        print(f"❌ 内容不一致 {len(differ)} 个：")
        for f in differ[:10]:
            print("   ", f)
    if missing or differ:
        return 1
    print(f"✅ 逐条核对通过：{len(files)} 个文件 blob sha 全部一致（{branch}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
