# -*- coding: utf-8 -*-
"""把 CI 诊断文件回传到 ci-logs 分支，供 contents API 读取。

为什么需要它（检验约束）：
  GitHub Actions 的 job 日志与 artifact 都托管在 Azure Blob
  （productionresultssa*.blob.core.windows.net），对脚本不可读；
  而 api.github.com 可读。所以把诊断写成仓库文件，走 contents API 取回，
  保证任何一次失败都能拿到真日志，而不是盲修。

每次运行写入 ci_diag/<run_number>/ 下，天然不冲突（无需先取旧 sha）。

双通道（任一成功即可读）：
  1. issue comment —— 主通道。artifact/job 日志在 Azure Blob 不可读，
     api.github.com 可读，因此把诊断正文作为评论追加到指定 issue。
  2. ci-logs 分支   —— 备通道，保留完整文件。
两个都失败时也不阻塞构建（本步骤 continue-on-error）。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"


def req(method: str, path: str, body: dict | None = None):
    # 检验教训：私有仓库 GITHUB_TOKEN 的写权限可能被 Actions 默认策略限制为 read，
    # 导致诊断静默写不进去（issue/分支写入都失败，却被 continue-on-error 掩盖）。
    # 因此优先使用仓库 secret OF_PAT（有 repo+workflow 写权限），GITHUB_TOKEN 仅作兜底。
    tok = (os.environ.get("OF_PAT")
           or os.environ.get("GITHUB_TOKEN")
           or os.environ.get("GH_TOKEN"))
    if not tok:
        return {"_err": "no token"}
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + tok)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "omegaforge-ci")
    try:
        return json.load(urllib.request.urlopen(r, timeout=60))
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"_err": type(e).__name__, "_body": str(e)[:200]}


def comment(repo: str, number: str, body: str) -> None:
    # GitHub 评论上限 65536 字符，超出截断（保留头尾更有价值的部分）
    if len(body) > 60000:
        body = body[:40000] + "\n\n... [截断] ...\n\n" + body[-15000:]
    r = req("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
    print("issue comment:", "OK" if r.get("id") else r)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run = os.environ.get("GITHUB_RUN_NUMBER", "0")
    diag = os.environ.get("OF_DIAG_DIR", "ci_diag")
    if not repo:
        print("无 GITHUB_REPOSITORY，跳过")
        return 0
    issue = os.environ.get("OF_DIAG_ISSUE", "1")
    print(f"repo={repo} diag={diag} issue={issue}")
    print(f"cwd 顶层条目: {sorted(os.listdir('.'))[:40]}")
    if not os.path.isdir(diag):
        msg = f"[run {os.environ.get('GITHUB_RUN_NUMBER','?')}] 无诊断目录 {diag}\ncwd 条目: {sorted(os.listdir('.'))[:40]}"
        print(msg)
        comment(repo, issue, "```\n" + msg + "\n```")
        return 0

    files = []
    for name in sorted(os.listdir(diag)):
        p = os.path.join(diag, name)
        if os.path.isfile(p):
            files.append((name, p))
    if not files:
        msg = f"[run {os.environ.get('GITHUB_RUN_NUMBER','?')}] 诊断目录 {diag} 为空"
        print(msg)
        comment(repo, issue, "```\n" + msg + "\n```")
        return 0

    # 确保 ci-logs 分支存在（不存在则基于当前 HEAD 创建）
    ref = req("GET", f"/repos/{repo}/git/ref/heads/ci-logs")
    if "_err" in ref:
        sha = os.environ.get("GITHUB_SHA", "")
        if not sha:
            print("无法创建分支：缺少 GITHUB_SHA")
            return 0
        created = req("POST", f"/repos/{repo}/git/refs", {"ref": "refs/heads/ci-logs", "sha": sha})
        print("创建 ci-logs 分支:", "OK" if "ref" in created else created)
    else:
        print("ci-logs 分支已存在")

    # 主通道：先把诊断正文发到 issue（不依赖分支写权限）
    run_id = os.environ.get("GITHUB_RUN_ID", "?")
    body = [f"## CI 诊断 · run {run_id} · {os.environ.get('GITHUB_RUN_NUMBER','?')}\n"]
    for name, p in files:
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception as e:
            txt = f"(读取失败: {e})"
        body.append(f"### {name}\n```\n{txt[:8000]}\n```\n")
    comment(repo, issue, "\n".join(body))

    # 备通道：ci-logs 分支
    for name, p in files:
        try:
            content = base64.b64encode(open(p, "rb").read()).decode()
        except Exception as e:
            print(f"读取失败 {name}: {e}")
            continue
        path = f"ci_diag/{run}/{name}"
        r = req("PUT", f"/repos/{repo}/contents/{path}",
                {"message": f"ci diagnostics run {run}: {name}",
                 "content": content, "branch": "ci-logs"})
        print(f"upload {path}:", "OK" if r.get("content") else r)
    print(f"诊断已回传，读取路径：/repos/{repo}/contents/ci_diag/{run}/<file>?ref=ci-logs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
