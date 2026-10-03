#!/usr/bin/env python3
"""真实端到端：默认端口被占用时，后端必须仍能起起来并被界面找到。

## 为什么需要这一层

单元用例用替身记录"绑定了哪个端口"，验的是分支逻辑；它拦不住下面这
件事——真实进程起在候选端口的下一个之后，按候选顺序探测的那一段到底
能不能连上。两者之间隔着一整个进程的启动开销，以及"占用是否在本平台
真的成立"。

用户视角的场景是具体的：上一次没退干净的残留进程占住默认端口。后端若
直接退出，界面只会反复显示"无法连接到本机服务"，使用者既退不掉占用程
序也换不了端口，应用等于不可用。

## 判定口径

* 真的占住默认端口（不带 SO_REUSEADDR：带上之后部分平台允许重复绑定，
  占用不成立，被观测的绑定会成功，症状是挂起而不是失败）；
* 真的以子进程启动后端，不替身；
* 按候选端口顺序探测，找到即通过；
* 占用不成立、进程未起、全部候选都不通，一律判失败——任何一步静默放行
  都会让本层退化成恒真。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8787


def backend_candidates(root: Path) -> list[int]:
    """从后端源码读候选端口，避免本脚本与实现各写一份。

    两边一旦漂移，这里验的就不是界面真正会去探测的那组端口。
    """
    import ast

    src = (root / "omegaforge" / "server.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and node.targets:
            if isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target != "PORT_CANDIDATES":
            continue
        if isinstance(value, (ast.Tuple, ast.List)):
            return [e.value for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, int)]
    raise SystemExit("后端未定义 PORT_CANDIDATES，无从探测")


def occupy(port: int) -> socket.socket | None:
    """占住端口。返回 None 表示未能占住（此时前提不成立）。"""
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    except OSError:
        pass
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def probe(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验证默认端口被占用时的退让")
    ap.add_argument("--root", default=".", help="仓库根目录")
    ap.add_argument("--timeout", type=float, default=30.0, help="探测总时长（秒）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    candidates = backend_candidates(root)
    if not candidates:
        print("FAIL 候选端口为空")
        return 1

    blocker = occupy(candidates[0])
    if blocker is None:
        print(f"FAIL 未能占住 {candidates[0]}：前提不成立，本层无从判定")
        return 1
    print(f"已占住 {candidates[0]}")

    home = tempfile.mkdtemp(prefix="of_portfallback_")
    env = dict(os.environ, OMEGAFORGE_HOME=home, PYTHONPATH=str(root))
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "omegaforge.server",
         "--port", str(candidates[0])],
        cwd=str(root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    out = ""
    found: int | None = None
    try:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            for port in candidates:
                if probe(port):
                    found = port
                    break
            if found is not None:
                break
            time.sleep(0.5)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    finally:
        blocker.close()

    print("---- 后端输出 ----")
    print((out or "").strip()[:800])

    if found is None:
        print(f"FAIL 候选端口 {candidates} 全部不通：退让未生效")
        return 1
    if found == candidates[0]:
        print(f"FAIL 探测到的是被占用的 {found}：占用未生效，本层未验到退让")
        return 1
    if "已改用" not in (out or ""):
        print(f"FAIL 退到 {found} 但未说明：使用者无从知道实际监听在哪个端口")
        return 1
    print(f"OK 默认端口被占用后实际监听 {found}，按候选顺序可探测到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
