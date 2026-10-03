#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CI 打出来的安装包取回沙盒，解包后独立复查布局与内容。

## 为什么需要这一条独立路径

CI 在 Windows runner 上做过安装验证，但那份结论只能由 runner 自己给出，
沙盒无从复核：GitHub 把 artifact 与 job 日志托管在 Azure Blob，对外下载
一律 403。唯一可取回的通道是 git blob API——故 CI 会把安装包提交到产物
分支，本脚本从那里取回。

解包用的是 7z（NSIS 自解压包），拿到的是安装后的目录布局，因此可以直接
跑与 CI 同一套 install_layout_checks，判定不会两边漂移。

## 用法

    python scripts/verify_installer_bundle.py [--repo owner/name] [--source manifest|release|branch]

## 取回通道

- manifest（默认）：只取产物分支上的清单（名称/体积/sha256）。体积小，
  任何网络条件下都能跑，用来对账"打的与验的是不是同一个包"。
- release：从 Release 资产下载安装包并解包复查布局。安装包内置语音后
  远超 git 单文件上限 100MB，产物分支已不再承载本体，取回只能走这里。
- branch：从产物分支 git blob 取回（仅适用于体积未超限的旧包）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import install_layout_checks as layout  # noqa: E402

API = "https://api.github.com"


def _token() -> str:
    for p in (os.path.join(os.path.dirname(ROOT), ".github_token"),
              os.path.join(ROOT, ".github_token"),
              os.environ.get("OF_GITHUB_TOKEN_FILE", "")):
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    return os.environ.get("OF_GITHUB_TOKEN", "")


def _get(url: str, token: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": "omegaforge-verify",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _from_manifest(args, token: str) -> int:
    """只取清单并对账：体积不符说明打的与验的不是同一个包。"""
    import base64
    try:
        item = json.loads(_get(f"{API}/repos/{args.repo}/contents/{args.dir}/manifest.json"
                               f"?ref={args.branch}", token))
        meta = json.loads(base64.b64decode(item["content"]).decode("utf-8"))
    except Exception as e:
        print(f"::error::取不到产物清单：{e} —— 读不到不能当作通过")
        return 1
    print("产物清单：", json.dumps(meta, ensure_ascii=False))
    if not meta.get("name") or not meta.get("bytes"):
        print("::error::清单缺少名称或体积")
        return 1
    if args.expect_bytes and int(meta["bytes"]) != args.expect_bytes:
        print(f"::error::体积对账不符：清单 {meta['bytes']} / 装机步骤 {args.expect_bytes}")
        return 1
    print("OK 产物清单体积对账通过")
    return 0


def _from_release(args, token: str) -> int:
    """从 Release 资产取回安装包本体并解包复查。"""
    st = f"{API}/repos/{args.repo}/releases/tags/{args.tag}"
    rel = json.loads(_get(st, token))
    assets = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")]
    if not assets:
        print(f"::error::Release {args.tag} 下没有 exe 资产 —— 读不到不能当作通过")
        return 1
    work = tempfile.mkdtemp(prefix="ofbundle_")
    try:
        rc = 0
        for a in assets:
            raw = _get(a["url"], token, "application/octet-stream")
            path = os.path.join(work, a["name"])
            with open(path, "wb") as f:
                f.write(raw)
            print(f"取回 {a['name']} · {len(raw)} 字节（Release 记录 {a['size']}）")
            if len(raw) != a["size"]:
                print(f"::error::下载体积不符：实得 {len(raw)} / 记录 {a['size']}")
                return 1
            out = os.path.join(work, "x_" + os.path.basename(path))
            os.makedirs(out, exist_ok=True)
            r = subprocess.run(["7z", "x", "-y", f"-o{out}", path],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"::error::解包失败 rc={r.returncode}\n{r.stdout[-800:]}")
                return 1
            files = sum(len(fs) for _d, _s, fs in os.walk(out))
            print(f"\n== {os.path.basename(path)} 解出 {files} 个文件 ==")
            problems = layout.check_layout(out)
            for p in problems:
                print(f"::error::{p}")
            if problems:
                rc = 1
            else:
                print("OK 布局与内容校验通过（主程序 / 运行时 / 前端产物）")
        return rc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="bin1732/omegaforge-buildtest")
    ap.add_argument("--branch", default="artifacts")
    ap.add_argument("--dir", default="installer")
    ap.add_argument("--tag", default="installer-latest")
    ap.add_argument("--source", default="manifest",
                    choices=["manifest", "release", "branch"])
    ap.add_argument("--expect-bytes", type=int, default=0,
                    help="装机步骤量到的安装包体积，用于两侧对账")
    args = ap.parse_args()

    token = _token()
    if not token:
        print("::error::读不到访问令牌")
        return 2

    if args.source == "release":
        return _from_release(args, token)
    if args.source == "manifest":
        return _from_manifest(args, token)

    listing = json.loads(_get(f"{API}/repos/{args.repo}/contents/{args.dir}?ref={args.branch}", token))
    exes = [x for x in listing if x["name"].lower().endswith((".exe", ".nsis.zip"))]
    if not exes:
        print(f"::error::{args.branch}:{args.dir} 下没有安装包 —— "
              f"产物未回传或已改名，读不到不能当作通过")
        return 1

    work = tempfile.mkdtemp(prefix="ofbundle_")
    try:
        targets = []
        for item in exes:
            raw = _get(f"{API}/repos/{args.repo}/git/blobs/{item['sha']}", token,
                       "application/vnd.github.raw")
            path = os.path.join(work, item["name"])
            with open(path, "wb") as f:
                f.write(raw)
            print(f"取回 {item['name']} · {len(raw)} 字节")
            if item["name"].lower().endswith(".exe"):
                targets.append(path)

        if not targets:
            print("::error::没有 exe 可解包")
            return 1

        for exe in targets:
            out = os.path.join(work, "x_" + os.path.basename(exe))
            os.makedirs(out, exist_ok=True)
            r = subprocess.run(["7z", "x", "-y", f"-o{out}", exe],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"::error::解包失败 rc={r.returncode}\n{r.stdout[-800:]}")
                return 1
            files = sum(len(fs) for _d, _s, fs in os.walk(out))
            print(f"\n== {os.path.basename(exe)} 解出 {files} 个文件 ==")
            problems = layout.check_layout(out)
            for p in problems:
                print(f"::error::{p}")
            if problems:
                return 1
            print("OK 布局与内容校验通过（主程序 / 运行时 / 前端产物）")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
