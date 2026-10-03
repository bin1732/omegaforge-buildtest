# -*- coding: utf-8 -*-
"""CI 诊断兜底回传（if: always()，无论成败都执行）。

为什么需要它（检验教训，勿删）：
  scripts/ci_publish_diag.py 在 CI 上出现过"静默写不进去"的情况——
  私有仓库 GITHUB_TOKEN 的写权限可能被 Actions 默认策略限制为 read，
  而该步骤带 continue-on-error，失败被掩盖成 success，于是连续多轮
  失败却拿不到任何日志，只能盲修。

  本脚本是最后一道防线：
  1. if: always() —— 快速失败导致后续步骤 skipped 时依然执行
  2. 只用 OF_PAT（仓库 secret，repo+workflow 写权限）—— 不依赖 GITHUB_TOKEN
  3. 不依赖任何第三方库，失败也要把失败本身报出来
  4. 环境自检一并回传（token 是否注入、ci_diag 是否有文件、平台信息）
"""
from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request

ISSUE = os.environ.get("OF_DIAG_ISSUE", "1")


def main() -> int:
    tok = os.environ.get("OF_PAT") or ""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run = os.environ.get("GITHUB_RUN_NUMBER", "?")
    run_id = os.environ.get("GITHUB_RUN_ID", "?")

    body = [
        f"## CI 诊断兜底 · run {run} (id {run_id})",
        f"- platform: `{platform.platform()}`",
        f"- python: `{sys.version.split()[0]}`",
        f"- OF_PAT: {'已注入 (len=%d)' % len(tok) if tok else '**未注入**'}",
    ]

    # ci_diag 内容
    diag = os.environ.get("OF_DIAG_DIR", "ci_diag")
    try:
        names = sorted(os.listdir(diag))
        body.append(f"- ci_diag 条目: `{names}`")
    except Exception as e:
        names = []
        body.append(f"- ci_diag 不可读: {e}")

    # 关键（检验教训，勿改回）：日志必须"取尾"。
    # Rust/前端编译错误出现在输出末尾，原先用 txt[:8000] 取头部，
    # 回传来的是一堆无害的下载/编译进度，真正的 error 全被截掉，
    # 直接导致连续多轮"有日志但看不出原因"。
    def excerpt(txt: str, head: int = 1500, tail: int = 7000) -> str:
        if len(txt) <= head + tail:
            return txt
        return (txt[:head]
                + f"\n...[省略 {len(txt) - head - tail} 字符, 错误通常在下方]...\n"
                + txt[-tail:])

    for n in names:
        p = os.path.join(diag, n)
        if not os.path.isfile(p):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception as e:
            txt = f"(读取失败: {e})"
        body.append(f"\n### {n}\n```\n{excerpt(txt)}\n```")

    # 关键：把 sidecar 构建产物的实际状态也报出来（判断 PyInstaller 是否真产出）
    for label, path in (("binaries", os.path.join("src-tauri", "binaries")),
                        ("_internal", os.path.join("src-tauri", "_internal"))):
        try:
            items = sorted(os.listdir(path))
            body.append(f"\n**{label}** ({len(items)} 项): `{items[:25]}`")
        except Exception as e:
            body.append(f"\n**{label}** 不可读: {e}")

    # 打包产物是否真的产出 —— 区分"编译失败"与"打包失败"
    for label, path in (("NSIS 产物", os.path.join("src-tauri", "target", "release", "bundle", "nsis")),
                        ("前端产物", os.path.join("frontend", "dist"))):
        try:
            items = sorted(os.listdir(path))
            body.append(f"\n**{label}** ({len(items)} 项): `{items[:25]}`")
        except Exception as e:
            body.append(f"\n**{label}** 不可读: {e}")

    text = "\n".join(body)
    if len(text) > 60000:
        text = text[:40000] + "\n\n...[截断]...\n\n" + text[-15000:]

    if not tok:
        print("OF_PAT 未注入，无法回传诊断")
        return 0
    if not repo:
        print("GITHUB_REPOSITORY 缺失")
        return 0

    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{ISSUE}/comments",
        data=json.dumps({"body": text}).encode(),
        method="POST",
    )
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "omegaforge-ci-diag")
    try:
        with urllib.request.urlopen(req, timeout=90) as f:
            print("diag comment OK", f.status)
    except urllib.error.HTTPError as e:
        print("diag comment FAIL", e.code, e.read()[:300].decode("utf-8", "replace"))
    except Exception as e:
        print("diag comment FAIL", type(e).__name__, e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(0)  # 兜底脚本绝不能让构建失败
