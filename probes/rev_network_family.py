#!/usr/bin/env python3
"""网络异常家族映射守卫的判定校验。

## 十个校验点：八个撤防线 + 一个反向 + 一个组合

 * A 撤掉 ConnectionError 分支 → 1 条
 * B 撤掉 HTTPException 分支 → 1 条
 * C 撤掉 BrokenPipeError 放行 → 1 条
 * D 撤掉 SSL 父类分支 → 1 条
 * E 撤掉 herror 分支 → 1 条
 * F 撤掉 OSError 的网络 errno 判定 → 2 条
 * G 撤掉非磁盘 OSError 集合 → 1 条
 * H 把 PermissionError 也改判成网络（防修过头）→ 2 条
 * I 同时撤 A 与 F → 4 条（含端到端）
 * J 基线

## I 为什么必须存在

端到端用例 `test_real_connection_reset_speaks_network`（起真 socket、发
RST、走完整客户端链路）在**只撤 A 或只撤 F 时都不红**：真实 RST 产生的
异常同时落在这两条分支上，撤掉任意一条，另一条照旧把它接住。

于是单锚点回退证明不了这条端到端守卫有效——它的说明里写着"只测
`_classify` 不够，异常必须在真实调用链上产生"，而这条最贵的守卫恰恰是
单锚点覆盖不到的那一条。要让它变红必须两条同撤，故校验点里必须有一个
组合锚点；只留单锚点的话，这条守卫失效不会被任何校验点发现。

## H 为什么是反向的

只验"撤掉修复会变红"证明不了修复刚好够用。H 把磁盘权限类也判成网络：
网络侧用例一条都不会红，只有"磁盘类必须保持原样"那两条会红。这类失效
不会被任何"撤掉修复"的锚点抓到。

## 备份不放 /tmp

备份与被注入文件同目录：/tmp 在命令之间会被回收，备份没了就无法还原，
注入会一直留在源码里。同一文件多处替换时只备份一次，否则后一次备份覆盖
前一次，还原回来的是已注入的版本。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_selfheal import (
    dirty_names,
    report_and_stop,
)
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_error_network_family.py"
C = TESTS + "::"
ERR = ROOT / "omegaforge" / "core" / "errors.py"

A = ('    if isinstance(exc, ConnectionError):\n'
     '        return CODE_NETWORK, "连接被中断或无法建立，请检查网络与接口地址"')
A_OFF = ('    if False:\n'
         '        return CODE_NETWORK, "连接被中断或无法建立，请检查网络与接口地址"')

B = ('    if isinstance(exc, _http_client.HTTPException):\n'
     '        return CODE_BAD_RESPONSE, "模型服务返回了不完整的响应"')
B_OFF = ('    if False:\n'
         '        return CODE_BAD_RESPONSE, "模型服务返回了不完整的响应"')

PIPE = ('    if isinstance(exc, BrokenPipeError):\n'
        '        return CODE_INTERNAL, "连接已断开，请重试"')
PIPE_OFF = ('    if False:\n'
            '        return CODE_INTERNAL, "连接已断开，请重试"')

SSL_P = ('    if isinstance(exc, _ssl.SSLError):\n'
         '        return CODE_NETWORK, "安全连接校验失败，请检查接口地址与证书"')
SSL_P_OFF = ('    if False:\n'
             '        return CODE_NETWORK, "安全连接校验失败，请检查接口地址与证书"')

HERR = ('    if isinstance(exc, _socket.herror):\n'
        '        return CODE_NETWORK, "无法解析主机地址，请检查网络与接口地址"')
HERR_OFF = ('    if False:\n'
            '        return CODE_NETWORK, "无法解析主机地址，请检查网络与接口地址"')

NETERR = ('        if getattr(exc, "errno", None) in _NET_ERRNOS:\n'
          '            return CODE_NETWORK, "网络不可达或连接被中断，请检查网络与接口地址"')
NETERR_OFF = ('        if False:\n'
              '            return CODE_NETWORK, "网络不可达或连接被中断，请检查网络与接口地址"')

NONDISK = ('        if isinstance(exc, (ChildProcessError, ProcessLookupError,\n'
           '                            InterruptedError)):\n'
           '            return CODE_INTERNAL, _DEFAULT_MSG')
NONDISK_OFF = ('        if False:\n'
               '            return CODE_INTERNAL, _DEFAULT_MSG')

DISK = ('        if isinstance(exc, PermissionError):\n'
        '            return CODE_INTERNAL, "读写失败，请检查运行目录权限"')
DISK_NET = ('        if isinstance(exc, PermissionError):\n'
            '            return CODE_NETWORK, "网络不可达或连接被中断，请检查网络与接口地址"')

CONN_ERRNO_T = C + "test_connection_error_without_errno_is_not_disk_permission"
CONN_T = C + "test_connection_errors_are_not_reported_as_disk_permission"
ERRNO_T = C + "test_network_errnos_map_to_network_code"
E2E_T = C + "test_real_connection_reset_speaks_network"

ANCHORS = [
    ("A 撤掉 ConnectionError 分支", [(ERR, A, A_OFF)], [CONN_ERRNO_T]),
    ("B 撤掉 HTTPException 分支", [(ERR, B, B_OFF)],
     [C + "test_incomplete_read_is_bad_response_not_internal"]),
    ("C 撤掉 BrokenPipeError 放行", [(ERR, PIPE, PIPE_OFF)],
     [C + "test_broken_pipe_keeps_connection_message"]),
    ("D 撤掉 SSL 父类分支", [(ERR, SSL_P, SSL_P_OFF)],
     [C + "test_unlisted_ssl_subclasses_still_reach_network"]),
    ("E 撤掉 herror 分支", [(ERR, HERR, HERR_OFF)],
     [C + "test_herror_is_address_resolution_not_disk"]),
    ("F 撤掉网络 errno 判定", [(ERR, NETERR, NETERR_OFF)], [CONN_T, ERRNO_T]),
    ("G 撤掉非磁盘 OSError 集合", [(ERR, NONDISK, NONDISK_OFF)],
     [C + "test_non_disk_oserrors_fall_to_generic_internal"]),
    ("H 磁盘类也判成网络（防修过头）", [(ERR, DISK, DISK_NET)],
     [C + "test_bare_oserror_and_permission_stay_disk_semantics",
      C + "test_disk_errors_keep_their_own_message"]),
    ("I 同时撤 A 与 F", [(ERR, A, A_OFF), (ERR, NETERR, NETERR_OFF)],
     [CONN_ERRNO_T, CONN_T, ERRNO_T, E2E_T]),
    ("J 基线", [], []),
]

FILES = [ERR]


def _restore(path: Path, bak: Path):
    shutil.move(str(bak), str(path))


def self_heal() -> bool:
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。

    但"残留"与"编写中的改动"在差异清单里完全同形，一律用 HEAD 覆盖会把
    当次正在写的源码一并抹掉。故这里只还原本脚本留下的 .bak，仍有差异时
    中止并交给人判断。
    """
    for path in FILES:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            _restore(path, bak)
            print(f"  [自愈] 还原 {path.name}")
    names = dirty_names(FILES, ROOT)
    if names:
        return report_and_stop(names)
    return True


def main() -> int:
    if not self_heal():
        return 1
    want = sys.argv[1:]
    bad = []
    for name, edits, expect in ANCHORS:
        if want and name[0] not in want:
            continue
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        missing = False
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次，"
                      f"须重定位（{path.name}）")
                bad.append(name)
                missing = True
                break
            if path not in paths:
                paths.append(path)
        if missing:
            continue

        baks = []
        try:
            for path in paths:
                bak = Path(str(path) + ".bak")
                shutil.copy2(path, bak)
                baks.append((path, bak))
            for path, old, new in edits:
                src = path.read_text(encoding="utf-8")
                path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed, tail = pytest_run(TESTS)
        finally:
            for path, bak in baks:
                if bak.exists():
                    _restore(path, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:56]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    ran = len([a for a in ANCHORS if not want or a[0][0] in want])
    print(f"本段校验点 {ran} 个，抓到 {ran - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
