# -*- coding: utf-8 -*-
"""覆盖安装（升级）后：用户数据必须还在，应用必须还能起来。

## 验的是什么

全新安装（先删目录再装）验不到升级路径。
就地覆盖安装时，目录里已经存着知识库、词条、待办与蒸馏产物，
安装包往同一个目录再装一遍。

这条路径上有两类失效，且都不会表现为"装不上"：

  1. 安装器把旧目录整棵清掉 —— 装得很好，用户数据没了；
     界面打开是空的，用户只会以为自己记错了。
  2. 覆盖后新旧文件混杂，sidecar 起不来 —— 用户双击没反应。

前者属于静默丢数据，后者属于装完用不了。

两者在"全新安装"的验收里都看不见，因为那里本来就没有数据可丢。

## 判定

  * 覆盖安装后目录仍存在、主程序与运行时仍在
  * 启动安装目录里的 sidecar，能监听
  * 覆盖前写进知识库的内容仍读得回来（标记串由 OF_UPGRADE_MARK 指定）

第三条是核心：只有它才能把"覆盖安装"与"全新安装"区分开。
没有它，前两条在全新安装上同样成立，整段验收会退化成恒真。

用法：
  python scripts/ci_verify_upgrade.py <安装目录> [端口]
  （环境变量 OF_UPGRADE_MARK 指定必须存活的内容，默认取验收写入串）
"""
from __future__ import annotations

import json
import os
import tempfile
import socket
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

DIAG = os.path.join(ROOT, "ci_diag")
MARK = os.environ.get("OF_UPGRADE_MARK", "验收写入的一条知识")


def log(lines: list[str], msg: str) -> None:
    print(msg, flush=True)
    lines.append(msg)


def probe(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(
        os.environ.get("OF_PORT", "8794"))
    timeout = int(os.environ.get("OF_SMOKE_TIMEOUT", "60"))

    os.makedirs(DIAG, exist_ok=True)
    lines: list[str] = []
    log(lines, f"覆盖安装验收目标: {root}  端口 {port}")

    import install_layout_checks as ilc  # noqa: PLC0415

    fails: list[str] = []
    bad = ilc.check_layout(root)
    if bad:
        for b in bad:
            log(lines, f"  [FAIL] {b}")
        fails.extend(bad)
    else:
        log(lines, "  [PASS] 覆盖后主程序 / 运行时 / 前端产物仍在")

    exe = ilc.find_file(root, ilc.SIDECAR_PREFIX, ".exe")
    if not exe:
        log(lines, "  [FAIL] 覆盖后找不到后端程序 —— 后续判定无从进行")
        fails.append("覆盖后找不到后端程序")
        with open(os.path.join(DIAG, "06-upgrade.txt"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return 1
    env = dict(os.environ)
    env["OF_PORT"] = str(port)
    kwargs: dict = {}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    # 日志不得写进安装目录：那是用户的目录，验收自己留下的文件会把
    # "卸载后还剩什么"的测量污染成看不清——残留里会混进我们自己写的
    # 东西，真正该报的运行时残留反而被淹没。
    out_path = os.path.join(tempfile.gettempdir(), "_upgrade_out.log")
    fh = open(out_path, "wb")
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=fh, stderr=subprocess.STDOUT,
                            env=env, **kwargs)

    ready = False
    for _ in range(timeout):
        time.sleep(1)
        if probe(port):
            ready = True
            break
        if proc.poll() is not None:
            break

    if not ready:
        fails.append("覆盖安装后的 sidecar 未能就绪")
        log(lines, "  [FAIL] 覆盖安装后的 sidecar 未能就绪")
        try:
            proc.kill()
        except Exception:
            pass
        fh.close()
        with open(out_path, "rb") as f:
            log(lines, "--- 后端输出 ---\n"
                       + f.read().decode("utf-8", "replace")[:3000])
    else:
        log(lines, "  [PASS] 覆盖安装后 sidecar 仍能启动")
        alive = proc.poll() is None
        if not alive:
            fails.append("端口可连但进程已退出 —— 连上的可能不是本 sidecar")
            log(lines, "  [FAIL] 端口可连但进程已退出")
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/kb/list",
                    timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            body = ""
            fails.append(f"读不回知识库列表：{e}")
            log(lines, f"  [FAIL] 读不回知识库列表：{e}")

        if MARK in body:
            log(lines, f"  [PASS] 覆盖前写入的内容仍在：{MARK!r}")
        else:
            fails.append(f"覆盖前写入的内容已丢失：{MARK!r}")
            log(lines, f"  [FAIL] 覆盖前写入的内容已丢失 —— "
                       f"响应为 {body[:200]!r}")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        if parsed in (None, {}, "", []):
            fails.append("知识库列表为空壳")
            log(lines, "  [FAIL] 知识库列表为空壳")

        try:
            proc.kill()
        except Exception:
            pass
        fh.close()

    log(lines, f"=== 覆盖安装验收 {'通过' if not fails else '未通过'} ===")
    for f in fails:
        log(lines, f"::error::{f}")
    with open(os.path.join(DIAG, "06-upgrade.txt"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
