# -*- coding: utf-8 -*-
"""CI 用 sidecar 真实启动快速测试（跨平台）。

为什么不用 bash /dev/tcp 探测（检验教训）：
  /dev/tcp 是 bash 的 netredir 特性，在 Windows 的 Git Bash(MSYS2) 上行为不确定；
  用错了会把"探测方式失效"误报成"产品启动失败"。
  这里改用 Python socket，Windows/Linux/macOS 行为一致，且已在 Linux 本地
  用真实 PyInstaller 产物验证通过。

验证内容（真实，不只看文件存在）：
  1. 按真实运行布局拼装：exe 与 _internal 必须同级
     （PyInstaller onedir 要求，否则报 Failed to load Python shared library）
  2. 真实启动 exe 进程
  3. socket 探测端口，直到就绪或超时
  4. 失败时输出完整诊断（stdout/stderr、进程状态、运行布局、端口占用）
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

# Windows CI 把 stdout 重定向到管道时默认编码可能是 cp1252/GBK，
# 一旦 print 中文即 UnicodeEncodeError —— 脚本会静默死亡且不写诊断。
# 这里强制 UTF-8 + replace，保证任何情况下都能把诊断写出来。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(ROOT, "src-tauri", "binaries")
INTERNAL = os.path.join(ROOT, "src-tauri", "_internal")
DIAG = os.path.join(ROOT, "ci_diag")
PORT = int(os.environ.get("OF_PORT", "8791"))
TIMEOUT = int(os.environ.get("OF_SMOKE_TIMEOUT", "60"))
SIDECAR = "omegaforge-backend"


def find_exe() -> str:
    ext = ".exe" if sys.platform.startswith("win") else ""
    if not os.path.isdir(BIN_DIR):
        _fatal(f"binaries 目录不存在: {BIN_DIR}")
    listing = sorted(os.listdir(BIN_DIR))
    for name in listing:
        if name.startswith(SIDECAR + "-") and name.endswith(ext):
            return os.path.join(BIN_DIR, name)
    _fatal("未找到 sidecar 产物\n"
           f"  BIN_DIR = {BIN_DIR}\n"
           f"  sys.platform = {sys.platform}\n"
           f"  期望后缀 = {ext!r}\n"
           f"  实际条目 = {listing}")


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


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(signal, "CTRL_BREAK_EVENT"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _fatal(msg: str) -> "NoReturn":  # noqa: F821
    """无论如何都要把诊断落盘 —— 上一版就是因为早退没写文件导致盲修一轮。"""
    try:
        os.makedirs(DIAG, exist_ok=True)
        with open(os.path.join(DIAG, "smoke.txt"), "w", encoding="utf-8") as f:
            f.write("FATAL\n" + msg + "\n")
    except Exception:
        pass
    print("FATAL " + msg)
    raise SystemExit(1)


def main() -> int:
    os.makedirs(DIAG, exist_ok=True)
    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        lines.append(msg)

    try:
        log(f"sys.platform: {sys.platform}")
        log(f"binaries 目录条目: {sorted(os.listdir(BIN_DIR))}")
    except Exception as e:
        log(f"(列目录失败: {e})")
    exe = find_exe()
    log(f"exe: {exe}")
    log(f"exe size: {os.path.getsize(exe)}")
    # 文件头校验：区分"真可执行二进制"与"占位/损坏文件"。
    # ELF=b'\\x7fELF' · PE=b'MZ' · MachO=b'\\xcf\\xfa\\xed\\xfe' 等
    try:
        with open(exe, "rb") as _f:
            head = _f.read(16)
        log(f"exe 文件头(hex): {head.hex()}  ascii={head[:4]!r}")
        magic_ok = head[:4] in (b"\x7fELF", b"MZ\x90\x00", b"MZ\x80\x00",
                                b"MZ\x00\x00", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
        log(f"exe 是可执行二进制: {magic_ok}")
        if not magic_ok:
            log("::error::exe 不是合法可执行二进制（占位文件？），sidecar 必然无法启动")
    except Exception as e:
        log(f"(读 exe 文件头失败: {e})")
    log(f"platform: {sys.platform} · python: {sys.version.split()[0]}")
    if not os.path.isdir(INTERNAL):
        log("::error::src-tauri/_internal 缺失")
        open(os.path.join(DIAG, "smoke.txt"), "w", encoding="utf-8").write("\n".join(lines))
        return 1
    log(f"_internal 条目数: {len(os.listdir(INTERNAL))}")
    try:
        items = sorted(os.listdir(INTERNAL))
        log(f"_internal 顶层清单: {items[:40]}")
    except Exception as e:
        log(f"(列 _internal 失败: {e})")

    tmp = tempfile.mkdtemp(prefix="of_smoke_")
    shutil.copy2(exe, tmp)
    shutil.copytree(INTERNAL, os.path.join(tmp, "_internal"))
    exe_name = os.path.basename(exe)
    log(f"运行布局 {tmp}: {sorted(os.listdir(tmp))}")

    out_path = os.path.join(tmp, "sidecar.log")
    fh = open(out_path, "wb")
    env = dict(os.environ)
    env["OF_PORT"] = str(PORT)
    kwargs = {}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([os.path.join(tmp, exe_name)], cwd=tmp,
                            stdout=fh, stderr=subprocess.STDOUT, env=env, **kwargs)

    ready = False
    started = time.time()
    for _ in range(TIMEOUT):
        time.sleep(1)
        if probe(PORT):
            ready = True
            break
        if proc.poll() is not None:
            log(f"进程已退出 returncode={proc.returncode} (t={time.time()-started:.1f}s)")
            break
    elapsed = time.time() - started
    kill(proc)
    time.sleep(0.5)
    fh.close()

    out = ""
    try:
        out = open(out_path, "rb").read().decode("utf-8", "replace")
    except Exception as e:
        out = f"(读取 sidecar.log 失败: {e})"

    log(f"耗时 {elapsed:.1f}s · 端口 {PORT} · ready={ready}")
    if ready:
        log("OK sidecar 真实启动并监听成功 ✅")
        log(f"--- sidecar 输出 ---\n{out[:2000]}")
        open(os.path.join(DIAG, "smoke.txt"), "w", encoding="utf-8").write("\n".join(lines))
        return 0

    # 失败：前台再跑一次抓 traceback
    log("===== sidecar 输出（后台）=====")
    log(out[:4000] or "(空)")
    log("===== 前台直接运行抓 traceback =====")
    try:
        r = subprocess.run([os.path.join(tmp, exe_name)], cwd=tmp, env=env,
                           capture_output=True, timeout=15)
        log(f"returncode={r.returncode}")
        log("stdout:\n" + r.stdout.decode("utf-8", "replace")[:3000])
        log("stderr:\n" + r.stderr.decode("utf-8", "replace")[:3000])
    except subprocess.TimeoutExpired as e:
        log(f"前台运行 15s 未退出（说明进程活着，属端口/绑定问题）: {e}")
    except Exception as e:
        log(f"前台运行异常: {type(e).__name__}: {e}")
    log("===== 端口占用 =====")
    try:
        import psutil

        log(f"psutil 可用: {psutil.__version__}")
    except Exception:
        log("psutil 不可用（不影响结论，仅少一项进程诊断）")
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        log(f"{PORT} 可绑定 → 未被占用")
    except OSError as e:
        log(f"{PORT} 绑定失败 → 可能残留占用: {e}")
    finally:
        s.close()
    open(os.path.join(DIAG, "smoke.txt"), "w", encoding="utf-8").write("\n".join(lines))
    log("::error::sidecar 未能在超时内监听端口")
    return 1


if __name__ == "__main__":
    import traceback
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # 任何未预期异常都必须落盘，绝不能静默死亡
        try:
            os.makedirs(DIAG, exist_ok=True)
            with open(os.path.join(DIAG, "smoke.txt"), "w", encoding="utf-8") as _f:
                _f.write("UNEXPECTED EXCEPTION\n" + traceback.format_exc())
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
