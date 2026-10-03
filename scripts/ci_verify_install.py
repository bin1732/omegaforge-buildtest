# -*- coding: utf-8 -*-
"""验证"安装后"的真实布局与可启动性。

为什么必须有这个脚本（而非只检查安装包存在）：
  Tauri 的 externalBin 只捆绑单个 exe，不会带上它的依赖目录。
  PyInstaller onedir 产物的 exe 必须与 _internal/ 同级，否则启动时报
    Failed to load Python shared library
  这个错误只在"安装后"的目录布局里才会暴露——源码目录里看起来一切正常。

  因此本脚本不做任何复制/拼装，而是在 NSIS 真实安装出来的目录里
  直接启动 sidecar 并探测端口：跑得起来，才证明布局真的对。

用法：
  python scripts/ci_verify_install.py <安装目录> [端口]
"""
from __future__ import annotations

import os
import tempfile
import socket
import subprocess
import sys
import time

# Windows 管道编码坑（与 sidecar_main.py 同源）：不强制 UTF-8 则打印中文即崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIAG = os.path.join(ROOT, "ci_diag")
SIDECAR = "omegaforge-backend"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install_layout_checks as layout  # noqa: E402


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


def find_layout(root: str) -> tuple[str, str]:
    """在安装目录递归定位 sidecar exe 与其同级 _internal。"""
    ext = ".exe" if sys.platform.startswith("win") else ""
    exe = ""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith(SIDECAR) and name.endswith(ext):
                exe = os.path.join(dirpath, name)
                break
        if exe:
            break
    if not exe:
        raise SystemExit(f"::error::安装目录未找到 {SIDECAR}*{ext}\n根目录 = {root}")
    return exe, os.path.join(os.path.dirname(exe), "_internal")


def _write(lines: list[str]) -> None:
    try:
        with open(os.path.join(DIAG, "04-install-verify.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("OF_PORT", "8792"))
    timeout = int(os.environ.get("OF_SMOKE_TIMEOUT", "60"))

    os.makedirs(DIAG, exist_ok=True)
    lines: list[str] = []

    log(lines, f"安装目录: {root}")
    log(lines, f"存在: {os.path.isdir(root)}")
    if not os.path.isdir(root):
        log(lines, "::error::安装目录不存在 —— 静默安装未生效")
        return 1

    exe, internal = find_layout(root)
    log(lines, f"sidecar exe : {exe}")
    log(lines, f"期望 _internal: {internal}")
    log(lines, f"exe size: {os.path.getsize(exe)}")

    if not os.path.isdir(internal):
        log(lines, "::error::_internal 与 exe 不同级 —— 运行时必报 Failed to load Python shared library")
        try:
            for dirpath, dirs, _files in os.walk(root):
                for d in dirs:
                    if d == "_internal":
                        log(lines, f"  发现 _internal 在: {os.path.join(dirpath, d)}")
        except Exception as e:
            log(lines, f"  (遍历失败: {e})")
        return 1
    log(lines, f"OK _internal 同级，条目数 {len(os.listdir(internal))}")

    # 布局之外的三条内容校验（主程序本体、Python 运行时、前端产物）。
    # 只验"sidecar 能启动"时这三条恒真：缺主程序、缺运行时、前端没打进去
    # 都不会影响后端进程起得来。
    ext = ".exe" if sys.platform.startswith("win") else ""
    problems = layout.check_layout(root, ext)
    for p in problems:
        log(lines, f"::error::{p}")
    if problems:
        _write(lines)
        return 1
    log(lines, "OK 布局与内容校验通过（主程序 / 运行时 / 前端产物）")

    # 端口占用会让"能连上"这条判定恒真：sidecar 绑定失败退出，而探测
    # 连上的是别的进程。启动前先确认端口是空的。
    if layout.port_in_use(port):
        alt = layout.free_port(port + 1)
        if not alt:
            log(lines, f"::error::端口 {port} 及其后若干端口均被占用 —— "
                       f"就绪判定会连到别的进程，不能作为就绪证据")
            _write(lines)
            return 1
        log(lines, f"端口 {port} 启动前已被占用，改用 {alt}")
        port = alt

    # 在真实安装位置启动 —— 不复制，就是要验证这个布局本身能跑
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
    out_path = os.path.join(tempfile.gettempdir(), "_verify_out.log")
    fh = open(out_path, "wb")
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=fh, stderr=subprocess.STDOUT, env=env, **kwargs)

    ready = False
    started = time.time()
    for _ in range(timeout):
        time.sleep(1)
        if probe(port):
            ready = True
            break
        if proc.poll() is not None:
            log(lines, f"进程退出 returncode={proc.returncode} (t={time.time()-started:.1f}s)")
            break

    # 存活状态必须在 kill 之前取：kill 之后 poll() 必然非 None，
    # 那时再判"进程已退出"会把每一次成功启动都报成失败，
    # 且报错写的是"布局或依赖有问题"，排查方向会被带偏。
    alive_at_ready = proc.poll() is None

    try:
        proc.kill()
    except Exception:
        pass
    time.sleep(0.5)
    fh.close()

    out = ""
    try:
        out = open(out_path, "rb").read().decode("utf-8", "replace")
    except Exception as e:
        out = f"(读取输出失败: {e})"

    log(lines, f"耗时 {time.time()-started:.1f}s · 端口 {port} · ready={ready}")
    log(lines, f"--- sidecar 输出 ---\n{out[:3000]}")

    if ready and not alive_at_ready:
        # 连上了、但在探测就绪时进程已经没了 —— 连到的不是本 sidecar
        log(lines, f"::error::端口可连但进程已退出 returncode={proc.returncode}"
                   f" —— 连上的不是本 sidecar")
        ready = False

    if ready:
        log(lines, "OK 安装后 sidecar 真实启动并监听成功 ✅")
        rc = 0
    else:
        log(lines, "::error::安装后 sidecar 未能就绪 —— 安装布局或依赖有问题")
        rc = 1

    _write(lines)
    return rc


if __name__ == "__main__":
    sys.exit(main())
