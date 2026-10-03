#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CI 打出来的安装包发布为 GitHub Release 资产。

## 为什么必须走 Release 而不是 git 分支

git 分支单文件上限 100MB，超出后 push 直接被拒（GH001）。安装包内置语音
运行库与模型后体积已远超该上限，产物分支这条回传通道因此不可用：push 失败
会让回传步骤报红，而"安装包已产出、装机验收全绿"的事实被这一步掩盖成整轮
失败；更糟的是把大文件塞进 git 历史后无法再取出。

Release 资产单文件上限 2GB，且自带一个可直接点击的下载地址——这正是人类
用户"下载安装包"的真实路径，而不是给机器用的 blob 通道。

## 设计约束（每条对应一种具体失效）

1. **上传后必须回读校验体积**。上传到一半断流会得到一个体积更小的资产，
   而 HTTP 层面仍可能返回成功；不回读的话发布步骤全绿，用户下载的却是
   一个装不上、且报错指向安装器本身的坏包。

2. **同名资产必须先删再传**。Release 上传同名资产会报 422；若把它当成
   "已存在即成功"放过，用户下载到的是旧包，而版本号看不出新旧差别。

3. **体积必须落进清单**。没有体积记录时，"内置语音后安装包涨了多少"只能
   靠猜，超限静默失败也无从提前发现。

4. **令牌缺失必须报错退出**。无声返回会让发布步骤显示成功而 Release 上
   什么都没有。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"

CHUNK = 1 << 20


def _token() -> str:
    for key in ("OF_PAT", "GITHUB_TOKEN", "OF_GITHUB_TOKEN"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    cand = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "..", ".github_token")
    if os.path.exists(cand):
        with open(cand, encoding="utf-8") as f:
            return f.read().strip()
    return ""


class GH:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.token = token

    def _req(self, url: str, method: str = "GET", body: bytes | None = None,
             ctype: str = "application/vnd.github+json", extra: dict | None = None):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "omegaforge-publish",
            "Content-type": ctype,
        }
        if extra:
            headers.update(extra)
        r = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                raw = resp.read()
            return resp.status, raw
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b"")

    def release_by_tag(self, tag: str):
        st, raw = self._req(f"{API}/repos/{self.repo}/releases/tags/{tag}")
        if st == 200:
            return json.loads(raw)
        if st == 404:
            return None
        raise RuntimeError(f"查询 Release 失败 HTTP {st}: {raw[:400]!r}")

    def create_release(self, tag: str, name: str, body: str):
        payload = json.dumps({"tag_name": tag, "name": name, "body": body,
                              "draft": False, "prerelease": False}).encode()
        st, raw = self._req(f"{API}/repos/{self.repo}/releases", "POST", payload)
        if st not in (200, 201):
            raise RuntimeError(f"创建 Release 失败 HTTP {st}: {raw[:400]!r}")
        return json.loads(raw)

    def assets(self, rel_id: int):
        st, raw = self._req(f"{API}/repos/{self.repo}/releases/{rel_id}/assets?per_page=100")
        if st != 200:
            raise RuntimeError(f"列出资产失败 HTTP {st}: {raw[:400]!r}")
        return json.loads(raw)

    def delete_asset(self, asset_id: int):
        st, raw = self._req(f"{API}/repos/{self.repo}/releases/assets/{asset_id}", "DELETE")
        if st not in (204, 404):
            raise RuntimeError(f"删除同名资产失败 HTTP {st}: {raw[:400]!r}")

    def upload(self, rel_id: int, path: str, name: str):
        size = os.path.getsize(path)
        url = f"{UPLOAD}/repos/{self.repo}/releases/{rel_id}/assets?name={urllib.parse.quote(name)}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "omegaforge-publish",
            "Content-type": "application/octet-stream",
            "Content-length": str(size),
        }
        with open(path, "rb") as f:
            r = urllib.request.Request(url, data=f, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(r, timeout=1800) as resp:
                    raw = resp.read()
                    st = resp.status
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"上传资产失败 HTTP {e.code}: {(e.read() or b'')[:400]!r}")
        if st not in (200, 201):
            raise RuntimeError(f"上传资产返回 {st}: {raw[:400]!r}")
        return json.loads(raw)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", default="installer-latest")
    ap.add_argument("--exe", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 安装包缺失必须判失败：静默跳过会让发布步骤显示成功而 Release 上什么
    # 都没有，用户找不到下载地址。
    if not os.path.isfile(args.exe):
        print(f"FAIL 安装包不存在：{args.exe}")
        return 1
    size = os.path.getsize(args.exe)
    print(f"安装包 {os.path.basename(args.exe)} · {size} 字节")

    digest = sha256_of(args.exe)
    print(f"sha256={digest}")

    # dry-run 必须在读令牌之前返回：dry-run 的目的是在**没有令牌的环境**
    # 里核对体积与摘要（本地、以及任何未注入密钥的地方）。令牌检查前置
    # 会让 dry-run 在缺令牌时退出 1，于是"能不能算出体积与 sha256"这件
    # 事被"有没有密钥"挡住，核对本身一次也没被执行。
    if args.dry_run:
        print("dry-run：不上传")
        return 0

    token = _token()
    if not token:
        print("FAIL 读不到访问令牌（OF_PAT / GITHUB_TOKEN）")
        return 1

    gh = GH(args.repo, token)
    rel = gh.release_by_tag(args.tag)
    if rel is None:
        rel = gh.create_release(args.tag, f"OmegaForge Studio 安装包（{args.tag}）",
                                "CI 每次全绿构建后更新。附件为 Windows 安装包，"
                                "下载后双击安装即可使用。")
        print(f"已创建 Release {args.tag}")
    rel_id = rel["id"]

    name = os.path.basename(args.exe)
    for a in gh.assets(rel_id):
        if a["name"] == name:
            gh.delete_asset(a["id"])
            print(f"已删除同名旧资产（{a['size']} 字节）")

    asset = gh.upload(rel_id, args.exe, name)
    up_size = asset.get("size", -1)
    # 必须回读校验：上传中断会产出体积更小的资产，而 HTTP 仍可能成功。
    if up_size != size:
        print(f"FAIL 上传体积不符：本地 {size} / 远端 {up_size}")
        return 1
    print(f"已上传 {name} · {up_size} 字节")
    print(f"下载地址：{asset.get('browser_download_url')}")
    print(f"体积记录：installer_bytes={size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
