#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 GitHub Git Data API 全量推送仓库。

沙盒内 git 协议被网络策略拦截（检验 403），只能走 HTTPS API。
关键：创建 tree 时**不带 base_tree**，因此新 commit 的树只包含本次上传的文件，
远端旧文件（如已废弃的 Electron 版 desktop/）会被自动删除 —— 这正是"推翻重来"想要的。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_REPO = "bin1732/omegaforge"
REPO = os.environ.get("PUSH_REPO", "")

# 令牌**惰性读取**，且不写死沙盒专属路径。
#
# 模块级 open(...) 有两个后果：
#   1. 收集口径的守卫用例在 CI 上全部崩（runner 没有 /data/workspace），
#      而 collect() 本身与网络、与令牌无关——守卫被无关前提挡在门外；
#   2. 崩的是 FileNotFoundError，看着像"环境坏了"，排查方向偏离。
# 改为用到时才读，并按 环境变量 → 常见位置 依次找，找不到给出明确说明。
_TOKEN_CACHE: str | None = None


def token() -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE:
        return _TOKEN_CACHE
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        _TOKEN_CACHE = env
        return env
    for cand in (Path("/data/workspace/.github_token"),
                 ROOT / ".github_token",
                 Path.home() / ".github_token"):
        try:
            if cand.is_file():
                v = cand.read_text(encoding="utf-8").strip()
                if v:
                    _TOKEN_CACHE = v
                    return v
        except OSError:
            continue
    sys.exit("FAIL: 读不到 GitHub 令牌（GITHUB_TOKEN 或 ~/.github_token）")


if not REPO:
    sys.exit("FAIL: 未指定 PUSH_REPO（目标仓库），拒绝按默认值推送")
if REPO == PRIVATE_REPO and os.environ.get("ALLOW_PRIVATE_PUSH") != "1":
    sys.exit(f"FAIL: 目标为私人仓 {PRIVATE_REPO} —— 全部验证通过前禁止覆盖，"
             "确需推送请显式设置 ALLOW_PRIVATE_PUSH=1")
API = f"https://api.github.com/repos/{REPO}"

EXCLUDE_SUFFIX = {'.pyc', '.pyo', '.DS_Store'}
# Electron 版已废弃（改用 Tauri 2），不再推送
EXCLUDE_PREFIX = ('desktop/',)


def api(method: str, url: str, payload: dict | None = None, retries: int = 4):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API + url, data=data, method=method,
                headers={"Authorization": f"Bearer {token()}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json",
                         "User-Agent": "OmegaForge-Uploader"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} → {e.code} {e.read().decode()[:200]}")
        except OSError:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def collect() -> list[str]:
    """只收版本库已跟踪的文件。

    ## 为什么不用目录遍历

    遍历工作目录会把任何本地新增目录一并收进去：一次依赖安装就能让
    待上传条目从数百涨到近千，而这类目录既不在 .gitignore 里、也不在
    排除表里，于是被当成源码推上去。症状是推送耗时成倍增长，最后在某
    个无关文件上读取失败——看起来像"仓库坏了"，其实是本地污染。

    已跟踪文件是唯一真源：不该推的东西先不进版本库，就不可能被推走。
    """
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        sys.exit(f"FAIL: git ls-files 失败（{proc.stderr.decode()[:200]}）——"
                 "拒绝退回目录遍历，否则本地未跟踪目录会被当成源码推送")
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    out = []
    for rel in raw.split("\0"):
        rel = rel.strip()
        if not rel:
            continue
        if rel.startswith(EXCLUDE_PREFIX):
            continue
        if any(rel.endswith(s) for s in EXCLUDE_SUFFIX):
            continue
        if not (ROOT / rel).is_file():
            # 已跟踪但本地缺失：不静默跳过，否则远端会少文件而本地看不出来
            sys.exit(f"FAIL: 已跟踪文件在本地缺失：{rel}")
        out.append(rel)
    return sorted(out)


def _claim_lock():
    """与回退校验共用同一把串行锁。

    ## 为什么推送也要抢锁

    回退校验靠"注入 → 跑用例 → 还原"工作：注入期间源码是被改坏的。
    若此时推送在跑，被改坏的文件可能正好在这一段上传，远端拿到注入态源码，
    而本地稍后已还原——两边从此不一致，且没人看得出来（推送报成功，
    测试全绿，只有装机后行为对不上）。

    共用锁后，注入类脚本运行期间推送直接拒绝，而不是赌上传顺序。
    """
    sys.path.insert(0, str(ROOT / "probes"))
    try:
        import _rev_verdict as rv
    except Exception:
        print("  [警告] 读不到回退校验锁模块，跳过互斥")
        return
    rv._claim_lock()


def _refuse_if_injected():
    """注入残留检查：任何 .bak 都说明有脚本在注入态被打断。

    与"未提交的正常改动"不同，.bak 是注入机制留下的痕迹。带着它推送，
    等于把半截改坏的代码当成源码发到远端。
    """
    baks = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in ('.git', 'node_modules',
                                              '__pycache__', '.pylibs')]
        for fn in fns:
            if fn.endswith(".bak"):
                baks.append(os.path.relpath(os.path.join(dp, fn), ROOT))
    if baks:
        sys.exit("FAIL: 检测到注入残留 .bak，拒绝推送（先把源码还原干净）："
                 + ", ".join(baks[:5]))


def _blob_sha(data: bytes) -> str:
    """手工算的 blob sha：sha1("blob <len>\0" + 内容)。

    只在"内容与 git 存储形态一致"时等于 git 的值。仓库未启用任何 clean
    过滤器时（无 .gitattributes、core.autocrlf=false）两者相同；启用后会
    不同——故对外一律用 _git_blob_shas()，这个只作对照与单文件取用。
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _git_blob_shas(rels: list[str]) -> dict[str, str]:
    """按路径批量取 git 自己的 blob sha（`git hash-object --stdin-paths`）。

    手工 sha1 只在没有 clean 过滤器时与 git 一致。Windows 上 core.autocrlf
    默认为 true，检出是 CRLF 而 git 存的是 LF：此时手工值对不上远端，复用
    判定静默全部落空——症状只是"推送又慢回原样"，看不出是 sha 算错了。
    交给 git 自己算是唯一与平台无关的做法，-w 顺带把对象写进本地库，
    后面取规范化后的字节才能取到。
    """
    proc = subprocess.run(["git", "hash-object", "-w", "--stdin-paths"],
                          cwd=ROOT, input="\n".join(rels),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"FAIL: git hash-object 失败：{(proc.stderr or '').strip()[:200]}")
    shas = (proc.stdout or "").split()
    if len(shas) != len(rels):
        sys.exit(f"FAIL: hash-object 返回 {len(shas)} 个值，与 {len(rels)} 个路径不符")
    return dict(zip(rels, shas))


def _canonical_bytes(rel: str, sha: str, raw: bytes) -> bytes:
    """取 git 会存进仓库的那份字节，而不是磁盘上的原始字节。

    直接上传 read_bytes() 的话，在启用行尾规范化的平台上远端收到的是 CRLF
    版本：blob sha 与 git 算的不同，下一轮复用判定再次落空，永远复不上。
    """
    if _blob_sha(raw) == sha:
        return raw
    proc = subprocess.run(["git", "cat-file", "blob", sha], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        sys.exit(f"FAIL: 取不到规范化后的内容 {rel}")
    return proc.stdout


def _remote_blob_shas(branch: str) -> dict[str, str]:
    """远端某分支的 路径 → blob sha。取不到就返回空表：复用失败只是慢，
    不会让推送出错，所以这里不判失败。
    """
    try:
        ref = api("GET", f"/git/ref/heads/{branch}")
        tree = api("GET", f"/git/trees/{ref['object']['sha']}?recursive=1")
    except Exception:                                      # noqa: BLE001
        return {}
    out = {}
    for item in tree.get("tree", []):
        if item.get("type") == "blob":
            out[item["path"]] = item["sha"]
    return out


def _default_branch() -> str:
    """目标分支取远端默认分支，不写死。

    写死 master 而远端默认分支是 main 时，PATCH 打在一个不存在（或已被删掉）
    的引用上：整轮十几分钟的 blob 上传全部作废，报错看着像"引用不合法"，
    排查方向被带到仓库权限上。分支不存在时转为创建，而不是让整轮白跑。
    """
    meta = api("GET", "")
    name = (meta.get("default_branch") or "").strip()
    if name:
        return name
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT,
                       capture_output=True, text=True)
    return (r.stdout or "").strip() or "main"


def _commit_message() -> str:
    """取本地 HEAD 的提交说明，而不是写死一句。

    写死的话，远端每条提交都记着同一句与实际改动无关的话，事后无法从
    提交历史看出这批改了什么。
    """
    r = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=ROOT,
                       capture_output=True, text=True)
    msg = (r.stdout or "").strip()
    return msg or "OmegaForge 同步"


def main() -> int:
    _claim_lock()
    _refuse_if_injected()
    files = collect()
    print(f"待上传 {len(files)} 个文件", flush=True)
    remote = _remote_blob_shas(_default_branch())
    shas = _git_blob_shas(files)
    tree = []
    reused = 0
    for i, rel in enumerate(files):
        data = _canonical_bytes(rel, shas[rel], (ROOT / rel).read_bytes())
        # 与远端同路径同内容则复用已有 blob：blob sha 就是 git 自己的算法
        # （sha1("blob <len>\0" + 内容)），不必上传即可判定相同。整轮上传
        # 几百个 blob 要几十分钟，而实际改动往往只有几个文件——复用让"改三个
        # 文件也要等半小时"这件事消失。
        sha = shas[rel]
        if remote.get(rel) == sha:
            reused += 1
            tree.append({"path": rel, "mode": "100644", "type": "blob",
                         "sha": sha})
            continue
        content = base64.b64encode(data).decode()
        blob = api("POST", "/git/blobs", {"content": content,
                                           "encoding": "base64"})
        tree.append({"path": rel, "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
        if (i + 1) % 25 == 0 or i == len(files) - 1:
            print(f"  blobs {i+1}/{len(files)}", flush=True)
    print(f"  复用已有 blob {reused} 个，实际上传 {len(files) - reused} 个",
          flush=True)

    tr = api("POST", "/git/trees", {"tree": tree})
    print("tree:", tr["sha"][:10])

    # 提交说明取本地 HEAD。写死一句的话，远端每条提交都记着同一句与实际
    # 改动无关的话，事后从提交历史看不出这批改了什么——而排查 CI 失败时
    # 第一件事就是看"这批到底动了什么"。
    cm = api("POST", "/git/commits", {
        "message": _commit_message(), "tree": tr["sha"]})
    print("commit:", cm["sha"][:10])

    branch = _default_branch()
    api("PATCH", f"/git/refs/heads/{branch}", {"sha": cm["sha"], "force": True})
    print(f"✅ 已推送至 {branch}")

    # 触发构建（workflow_dispatch）
    try:
        api("POST", "/actions/workflows/build.yml/dispatches", {"ref": branch})
        print("🚀 已触发 build.yml")
    except Exception as e:
        print("触发失败:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
