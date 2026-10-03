#!/usr/bin/env python3
"""把本地版本库全量覆盖推送到远端主分支。

## 为什么不用 git push

沙盒出口网关对 github.com 主域一律拒绝，git 协议不可用，只有
api.github.com 可达。因此走 Git Data API：逐批建 tree → 建 commit →
强制更新分支引用。

## 覆盖语义

提交的 parent 置空，形成新根；旧历史不再出现在分支上，实现完全覆盖。

用法：python3 scripts/push_force_cover.py [--dry-run]
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PRIVATE_REPO = "bin1732/omegaforge"
REPO = os.environ.get("PUSH_REPO", "")
BRANCH = os.environ.get("PUSH_BRANCH", "master")
API = f"https://api.github.com/repos/{REPO}"
DRY = "--dry-run" in sys.argv
# 目标仓库缺失时只能在入口拒绝，不能在导入期退出：本模块被用例导入（那是
# 校验它自身行为的方式），导入即 sys.exit 会让整组用例以 SystemExit 失败，
# 报出来的是 19 条"未指定 PUSH_REPO"，看起来像配置没配，实际是本模块把自
# 己的调用方一起挡在了门外。
ROOT = os.environ.get("REPO_ROOT", "/data/workspace/LATEST")
# 增量推送默认开启：全量重建要随 tree 上传每一个文件，数据量随仓库规模
# 增长，被中断时远端仍是旧内容，表现为"推送失败"而非"没走完"。增量只
# 提交差异条目。安全性由推送后的全量 blob 逐条校验兜底——基底若带污染，
# 校验环节会把缺失/多余/不符逐条列出来。
# 需要强制全量时设 PUSH_FULL=1。
INCREMENTAL = os.environ.get("PUSH_FULL", "") != "1"


_token = None


def token():
    """读取访问令牌。

    刻意延迟到实际发起请求时才读：模块被导入（例如被用例加载以测试比对
    逻辑）时不该因为缺少凭据文件或路径不同而崩溃。
    """
    global _token
    if _token is None:
        _token = open(os.environ.get(
            "GITHUB_TOKEN_FILE", "/data/workspace/.github_token")).read().strip()
    return _token


def api(method, path, payload=None, raw=False):
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"{method} {path} 失败 {e.code}: {detail}")
        except Exception as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"{method} {path} 异常: {e}")
    return {}


def resolve_branch() -> str:
    """确定要写的分支。

    显式给了 PUSH_BRANCH 就用它；否则取仓库的默认分支。写死 master 时，
    默认分支是 main 的仓库会一路走到最后一步才失败：文件已全部上传，而
    "更新引用"对不存在的引用返回 422，整轮上传白做。
    """
    if os.environ.get("PUSH_BRANCH"):
        return os.environ["PUSH_BRANCH"]
    try:
        info = api("GET", "")
        return info.get("default_branch") or BRANCH
    except SystemExit:
        return BRANCH


def update_ref(branch: str, sha: str) -> None:
    """更新分支引用；引用不存在时先创建。

    对不存在的引用做 PATCH 返回 422 "Reference does not exist"，而这条
    消息读起来像"分支名写错了"，不说"这个分支还没有"。分支是新建仓库里
    常见的情况，因此 422 时转为创建。
    """
    try:
        api("PATCH", f"/git/refs/heads/{branch}",
            {"sha": sha, "force": True})
        return
    except SystemExit as exc:
        if "422" not in str(exc):
            raise
    api("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})


def diff_trees(local, remote):
    """比对本地与远端的（路径 -> blob sha）映射。

    返回（远端缺失, 远端多余, 内容不一致）。逐条比对 sha 而不是比数量：
    **文件条数相同与内容相同是两回事**。上传环节若把内容换掉（例如按文本
    而非二进制提交），条数依然吻合，只有 sha 会暴露。
    """
    missing = sorted(set(local) - set(remote))
    extra = sorted(set(remote) - set(local))
    mismatched = sorted(k for k in set(local) & set(remote)
                        if local[k] != remote[k])
    return missing, extra, mismatched


def local_tree():
    """本地 HEAD 的（路径 -> blob sha）映射。"""
    out = git("ls-tree", "-r", "-z", "HEAD")
    m = {}
    for line in out.split("\0"):
        if not line:
            continue
        meta, _, path = line.partition("\t")
        sha = meta.split()[2]
        m[path] = sha
    return m


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args],
                          capture_output=True, text=True, check=True).stdout


def fetch_tree(sha):
    """取一棵树的（路径 -> blob sha）映射，并报告它是否完整。

    递归取树的接口在条目过多时返回 truncated=true，此时 tree 只是**一部分**。
    返回值必须带上这个标记：把截断的清单当成完整清单，会把"接口没返回的文件"
    读成"本地已删除的文件"，进而在增量推送里触发批量删除——后果是远端整片
    文件消失，而脚本只报一句"需删除 N 个"。
    """
    tr = api("GET", f"/git/trees/{sha}?recursive=1")
    entries = {x["path"]: x.get("sha")
               for x in tr.get("tree", [])
               if x.get("type") == "blob"}
    return entries, not tr.get("truncated")


def remote_tree(branch):
    """远端分支当前的树。

    返回（base_tree_sha, 映射, 完整与否）。分支不存在或清单被截断时映射
    为空且标记为不完整，调用方据此退化为全量——全量重建不依赖基底，不会
    因为基底不全而误删。
    """
    try:
        ref = api("GET", f"/git/ref/heads/{branch}")
        commit = api("GET", f"/git/commits/{ref['object']['sha']}")
    except SystemExit:
        return None, {}, False
    try:
        entries, complete = fetch_tree(commit["tree"]["sha"])
    except SystemExit:
        return None, {}, False
    return commit["tree"]["sha"], entries, complete


def plan_push(files, local, remote, complete, incremental=True):
    """决定这轮要更新/删除哪些条目，以及是否以远端树为基底。

    返回（todo, gone, use_base）。

    关键约束：只有**完整的**远端清单才能用来算删除项。清单被接口截断时，
    "没返回"不等于"本地没有"，据此删会把接口没列出的文件整片删掉，而脚本
    只会报一句"需删除 N 个"，看不出是批量误删。因此不完整时一律走全量：
    全量不带基底，重建出来的树只含本次上传的内容，不依赖对远端的判断。
    """
    if incremental and complete and remote:
        todo = [p for p in files if remote.get(p) != local.get(p)]
        gone = [p for p in remote if p not in local]
        return todo, gone, True
    return list(files), [], False


def working_tree_dirty():
    """返回工作区里与 HEAD 不一致的条目。

    本地 tree 取自 HEAD，而意图交付的是工作区。两者不一致时，比对会拿已
    提交的内容去比远端，得出"无差异"并静默跳过——未提交的修复永远推不上
    去，而输出上看不出来：条数与"已推送"完全相同。
    """
    out = git("status", "--porcelain", "-z").split("\0")
    return [e for e in out if e.strip()]


def main():
    if not REPO:
        print("[拒绝] 未指定 PUSH_REPO（目标仓库），拒绝按默认值推送")
        print("[提示] 显式设置 PUSH_REPO=owner/repo 后再跑")
        return 2
    # 交付目标是私人仓时必须显式放行。默认即私人仓意味着一次误调用就会在
    # 验证完成前覆盖交付物，而覆盖是强制的、没有回退。
    if REPO == PRIVATE_REPO and os.environ.get("ALLOW_PRIVATE_PUSH") != "1":
        print(f"[拒绝] 目标为私人仓 {PRIVATE_REPO} —— "
              "全部验证通过前禁止覆盖")
        print("[提示] 确需推送请显式设置 ALLOW_PRIVATE_PUSH=1")
        return 2
    # 工作区必须干净：本地 tree 取自 HEAD，未提交的改动既推不上去、也不会
    # 被报出来，表现为"推送成功但实际是旧内容"。这种不一致无法从输出察觉，
    # 只能在入口拦住。
    dirty = working_tree_dirty()
    if dirty and "--allow-dirty" not in sys.argv:
        print("[拒绝] 工作区与 HEAD 不一致，未提交的改动不会进入本次推送：")
        for e in dirty[:20]:
            print("   ", e)
        if len(dirty) > 20:
            print(f"    ... 另有 {len(dirty) - 20} 项")
        print("[提示] 先提交再推送；确知自己在做什么时加 --allow-dirty")
        return 2

    # -z 保证中文文件名不被转义成八进制，否则文件会被判为不存在而跳过
    files = [f for f in git("ls-files", "-z").split("\0") if f]
    print(f"[本地纳入版本控制的文件] {len(files)}")

    # 全量重建时每个文件都要随 tree 上传一次，总量一大就会被中断在中途，
    # 而中断时远端仍是旧内容——看起来像"推送失败"，实际是没走完。
    # 增量模式下以远端现有 tree 为基底，只提交差异条目，数据量与改动
    # 规模成正比，与仓库规模无关。
    target = resolve_branch()
    rtree_sha, remote, complete = (remote_tree(target) if INCREMENTAL
                                   else (None, {}, False))
    local = local_tree()
    todo, gone, use_base = plan_push(files, local, remote, complete,
                                     INCREMENTAL)
    if use_base:
        print(f"[增量] 远端已有 {len(remote)}；需更新 {len(todo)}，"
              f"需删除 {len(gone)}")
        if not todo and not gone:
            print("[增量] 无差异，不写远端")
            return 0
    else:
        if INCREMENTAL:
            if remote and not complete:
                print(f"[增量] 远端树被接口截断（只返回 {len(remote)} 个），"
                      f"不足以据此判断删除项，退化为全量")
            else:
                print("[增量] 远端分支不存在或为空，退化为全量")
        print(f"[全量] 重传 {len(todo)} 个，不带基底")

    head = git("rev-parse", "HEAD").strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()

    blobs = []
    BATCH = 100
    # 增量时以远端现有 tree 为基底，未改动的文件随基底带入，不必重传。
    # 全量时基底必须为 None：带基底的"全量"只是覆盖差异，基底里那些本地
    # 已没有的文件会留在远端，表现为删了却还在。
    base_tree = rtree_sha if use_base else None
    # 删除项以 sha=None 表达：本地已移除而远端仍在的文件，不显式删会
    # 一直留在远端，表现为"删了但远端还有"。
    if gone:
        payload = {"tree": [{"path": p, "mode": "100644",
                             "type": "blob", "sha": None} for p in gone]}
        if base_tree:
            payload["base_tree"] = base_tree
        base_tree = api("POST", "/git/trees", payload)["sha"]
        print(f"  删除批次: {len(gone)} 个")
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        tree = []
        missing = []
        for rel in chunk:
            fp = os.path.join(ROOT, rel)
            if not os.path.isfile(fp):
                missing.append(rel)
                continue
            with open(fp, "rb") as f:
                raw = f.read()
            # tree API 的 content 收文件内容本身，不是 base64。
            # 早先误用 base64 编码，导致远端每个文件都被存成 base64 文本
            # （体积膨胀约 1/3，且内容与源码不符）。
            if b"\0" in raw[:8000]:
                # 二进制文件先建 blob，再以 sha 引用
                b = api("POST", "/git/blobs", {
                    "content": base64.b64encode(raw).decode(),
                    "encoding": "base64",
                })
                tree.append({"path": rel, "mode": "100644",
                             "type": "blob", "sha": b["sha"]})
            else:
                tree.append({
                    "path": rel,
                    "mode": "100755" if os.access(fp, os.X_OK) else "100644",
                    "type": "blob",
                    "content": raw.decode("utf-8"),
                })
        if missing:
            print(f"  [跳过不存在的文件] {len(missing)}: {missing[:3]}")
        payload = {"tree": tree}
        if base_tree:
            payload["base_tree"] = base_tree
        res = api("POST", "/git/trees", payload)
        base_tree = res["sha"]
        blobs.extend(tree)
        print(f"  批次 {i // BATCH + 1}: 累计 {len(blobs)} 个，tree={base_tree[:12]}")

    # 提交说明取自本地 HEAD，不以常量写死：写死的话远端每条提交都记着同一
    # 句话，与实际改动无关，事后无法从提交说明判断这批改了什么。
    commit_msg = git("log", "-1", "--pretty=%B").strip() or "全量同步"
    if DRY:
        print(f"[预演] 将建 commit，文件 {len(blobs)} 个，不写远端")
        return 0

    commit = api("POST", "/git/commits", {
        "message": commit_msg,
        "tree": base_tree,
        "parents": [],  # 新根：旧历史不再出现在分支上
    })
    print(f"[commit] {commit['sha'][:12]}  parents={commit.get('parents')}")

    update_ref(target, commit["sha"])
    print(f"[分支] {target} -> {commit['sha'][:12]}（强制覆盖）")

    # 校验：逐条比对 blob sha。只比数量会放过内容被换掉的上传——条数吻合
    # 不代表内容吻合，而 blob sha 由内容决定，全等即内容全等。
    ref = api("GET", f"/git/ref/heads/{target}")
    remote, complete = fetch_tree(ref["object"]["sha"])
    local = local_tree()
    if not complete:
        # 清单不完整时"缺失"是假象：接口没返回不等于远端没有。此时既不能
        # 判一致（会把没验过的部分当成验过），也不能报缺失（会把截断读成
        # 推送丢文件），必须单独说明。
        print(f"[校验] 远端树被接口截断，只拿到 {len(remote)} 个条目，"
              f"无法逐条校验（本地 {len(local)} 个）")
        return 1
    missing, extra, mismatched = diff_trees(local, remote)
    print(f"[校验] 本地 {len(local)} 个 / 远端 {len(remote)} 个")
    if missing or extra or mismatched:
        print(f"[校验] 不一致：远端缺失 {len(missing)}、远端多余 {len(extra)}、"
              f"内容不符 {len(mismatched)}")
        for k in (missing + extra + mismatched)[:20]:
            print(f"  {k}: 本地 {local.get(k, '-')[:12]} / 远端 {remote.get(k, '-')[:12]}")
        return 1
    print(f"[校验] 全部 {len(local)} 个文件 blob sha 逐条一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
