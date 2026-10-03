#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把安装包清单回传到产物分支（走 Git Data API，不做 checkout）。

## 为什么不能用 git checkout

前端构建会改动 frontend/package-lock.json。产物分支步骤排在它之后，
`git checkout artifacts` 会因为"本地改动会被覆盖"直接中止：

    error: Your local changes to the following files would be
    overwritten by checkout: frontend/package-lock.json

而 checkout 失败发生在写清单之前，于是本步骤报的是 git 的错，真实意图
（把这一轮包多大、校验和是多少留下来供复查）完全没达成——失败那轮恰恰最
需要这条信息。改用 API 直接提交单个文件，不碰工作区，与工作区脏不脏无关。

## 为什么分支上必须留东西

安装包走 Release 资产回传（git 单文件上限 100MB，内置语音后远超）。分支
上若什么都不留，"这一轮到底打没打出包"就无从复查。清单是对账依据：装机
步骤量到的体积与这里记录的不一致，说明打的与验的不是同一个包。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

RETRIES = 5


def api(method: str, url: str, payload=None, token: str = "", base: str = ""):
    data = json.dumps(payload).encode() if payload is not None else None
    last = ""
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                base + url, data=data, method=method,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json",
                         "User-Agent": "OmegaForge-Manifest"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            last = f"{e.code} {body}"
            if e.code == 404:
                return None
            if e.code in (500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last}")
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < RETRIES - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last}")
    raise RuntimeError(f"{method} {url} -> {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="artifacts")
    ap.add_argument("--file", required=True, help="本地清单文件路径")
    ap.add_argument("--path", default="installer/manifest.json",
                    help="分支上的目标路径")
    args = ap.parse_args()

    # 与 publish_installer_release.py 读取同一组变量名：只认其中一个的话，
    # 令牌在工作流里以另一个名字注入时脚本会报"未设置令牌"——一次白跑一轮
    # 构建，而日志里只剩一句与真实意图无关的话。
    token = ""
    for key in ("OF_PAT", "GITHUB_TOKEN", "OF_GITHUB_TOKEN", "GH"):
        v = os.environ.get(key, "").strip()
        if v:
            token = v
            break
    if not token:
        print("FAIL 读不到访问令牌（OF_PAT / GITHUB_TOKEN / GH）")
        return 1
    if not os.path.isfile(args.file):
        print(f"FAIL 清单文件不存在：{args.file}")
        return 1

    # GH_API_BASE 供用例在本地起假 API 服务端到端验证：这一层若只能靠 CI 试错，
    # 一次写错的代价是一整轮构建，而失败原因在日志里也只剩一句 git 的错。
    base = (os.environ.get("GH_API_BASE")
            or f"https://api.github.com/repos/{args.repo}")
    content = open(args.file, "rb").read()
    if not content.strip():
        print(f"FAIL 清单文件为空：{args.file}")
        return 1
    try:
        json.loads(content.decode("utf-8"))
    except Exception as e:
        print(f"FAIL 清单不是合法 JSON：{e}")
        return 1

    blob = api("POST", "/git/blobs",
               {"content": base64.b64encode(content).decode(),
                "encoding": "base64"}, token, base)
    entry = {"path": args.path, "mode": "100644", "type": "blob",
             "sha": blob["sha"]}

    ref = api("GET", f"/git/ref/heads/{args.branch}", token=token, base=base)
    if ref is None:
        # 分支不存在：建孤儿提交，不带父提交，避免把源码树带进来。
        tr = api("POST", "/git/trees", {"tree": [entry]}, token, base)
        cm = api("POST", "/git/commits",
                 {"message": "installer manifest", "tree": tr["sha"],
                  "parents": []}, token, base)
        api("POST", "/git/refs", {"ref": f"refs/heads/{args.branch}",
                                  "sha": cm["sha"]}, token, base)
        print(f"OK 已创建 {args.branch}（孤儿分支）并写入 {args.path}")
        return 0

    remote_sha = ref["object"]["sha"]
    # base_tree 会自动带上分支已有内容，只覆盖目标路径这一条。
    tr = api("POST", "/git/trees",
             {"base_tree": remote_sha, "tree": [entry]}, token, base)
    cm = api("POST", "/git/commits",
             {"message": "installer manifest", "tree": tr["sha"],
              "parents": [remote_sha]}, token, base)
    api("PATCH", f"/git/refs/heads/{args.branch}", {"sha": cm["sha"]},
        token, base)

    # 回读校验：POST 成功但分支上没这个文件，等于没写，而脚本会显示成功。
    check = api("GET",
                f"/contents/{args.path}?ref={args.branch}",
                token=token, base=base)
    if not check or check.get("sha") != blob["sha"]:
        print(f"FAIL 回读不一致：分支上 {args.path} 内容与本地清单不同")
        return 1
    print(f"OK 已回传 {args.path} -> {args.repo} {args.branch} "
          f"（{len(content)} 字节，回读一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
