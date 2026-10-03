# -*- coding: utf-8 -*-
"""OmegaForge 后端 sidecar 入口（由 PyInstaller 打包为 omegaforge-backend）。

注意：必须保留 stdout 能力（不要用 --windowed 打包），
因为 Tauri 通过管道捕获 stdout 转发到 Rust 日志。
控制台窗口的隐藏由 Rust 侧 creation_flags 负责，而非这里。
"""
from __future__ import annotations

import io
import os
import sys


def _force_utf8() -> None:
    """强制 stdout/stderr 为 UTF-8 —— 必须在任何 print 之前调用。

    检验教训（Windows 专属坑，勿删）：
      Windows 控制台/管道默认编码是 cp1252（charmap），而项目里有 15 处
      print 含非 ASCII 字符（Ω、→、⚠、═ 以及中文日志）。在 Linux(UTF-8) 上
      一切正常，打包到 Windows 后一旦打印就抛：
        UnicodeEncodeError: 'charmap' codec can't encode character '\\u03a9'
      导致 sidecar 进程启动即退出（returncode=1），CI 快速连续 19 次失败。

    errors="replace" 是最后兜底：即便对端编码仍不支持，也只丢字符不崩溃。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception:
            pass
        try:
            setattr(sys, name, io.TextIOWrapper(
                getattr(stream, "buffer", stream),
                encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


_force_utf8()


def _port() -> int:
    raw = os.environ.get("OF_PORT", "8787")
    try:
        return int(raw)
    except ValueError:
        return 8787


def main() -> int:
    # 打包后 cwd 可能在任意位置，确保 import 能找到 omegaforge 包
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    from omegaforge.server import serve

    port = _port()
    # 关键：立即 flush，让 Tauri 的健康检查与日志转发尽快拿到输出
    print(f"[sidecar] starting on 127.0.0.1:{port}", flush=True)
    try:
        serve(host="127.0.0.1", port=port)
    except KeyboardInterrupt:
        return 0
    except SystemExit as exc:
        # 绑定失败时 serve 已给出中文说明，这里原样透出，
        # 不再回显原始异常：英文技术串照着无从下手。
        print(f"[sidecar] 启动失败：{exc}", flush=True)
        return 1
    except Exception as exc:
        # 未预期的编程错误：完整调用栈只进内部日志，标准错误输出只留一句
        # 中文。否则英文调用栈连同本机路径会经外壳转发进他人可见的日志。
        from omegaforge.core.errors import record_internal_error

        record_internal_error(exc, "sidecar.main")
        print("[sidecar] 服务遇到未预期的错误，详细信息已记入数据目录下的"
              "日志文件 errors.log", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
