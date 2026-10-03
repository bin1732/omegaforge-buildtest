#!/usr/bin/env python3
"""CORS 预检与拒绝留痕守卫的判定校验：撤掉修复，指定的用例必须变红。

## 为什么要点名到用例

只比对"有没有失败项"区分不了两件事：失败的是不是这一处守卫对应的那条
用例。给被测模块加一个模块级未定义名，整片用例都会失败，任何"有失败"
的判定都会被满足——那时的红是环境故障，不是守卫在起作用。

因此每个校验点都点名预期变红的用例，并且严格：出现预期之外的失败项同样
判未抓到。

## 两类校验点缺一不可

A、B 是"撤掉修复"方向，证明修复有用；C~E 是"防修过头"方向，证明修复刚好
够用。只留前一类的话，把来源校验放宽成一律放行、或收紧成一律拒绝，同样
报全绿——拦截确实发生了，只是方向反了。这类失效不会被任何"撤掉"方向的
校验点发现。

## 点名的来源

每个点名清单都要逐个注入确认。照着注入点推出来的清单会漏掉连带失败的
那几条，而漏掉的后果是：注入生效了，判定却说"预期之外还失败"。

用法::

    python3 probes/rev_cors.py          # 全部校验点
    python3 probes/rev_cors.py A C      # 只跑指定校验点
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT", "/data/workspace/LATEST"))
sys.path.insert(0, str(ROOT / "probes"))

from _rev_verdict import pytest_run, reg_add, restore_src, verdict  # noqa: E402

SRC = ROOT / "omegaforge" / "server.py"
TESTS = "tests/test_cors_preflight.py"
BACKUP = Path("/data/workspace/.rev_backups/server.py.bak")

_P = "TestCorsPreflight"
_Q = "TestRejectedRequestLeavesNoTrace"


def case(kls: str, name: str) -> str:
    return f"{TESTS}::{kls}::{name}"


# 长度校验块与建会话块在源码里相邻，交换两者即还原"先建会话后校验"的旧顺序。
_CHECK = (
    '    if len(msg) > MSG_MAX_CHARS:\n'
    '        raise UserError(\n'
    '            f"这条消息过长（{len(msg):,} 字符，上限 {MSG_MAX_CHARS:,}），"\n'
    '            "请拆分后再发送")\n'
)
_MAKE = (
    '    cid = str(payload.get("conversation_id") or "")\n'
    '    if not cid:\n'
    '        conv = CONVS.new(title=msg[:24])\n'
    '        cid = conv["id"]\n'
    '    else:\n'
    '        conv = CONVS.get(cid)\n'
    '        if not conv:\n'
    '            # 两条路径都必须报错：静默新建会让用户以为历史还在，实际已被换掉。\n'
    '            raise UserError("未找到该对话，可能已被清理")\n'
)

# (标识, 说明, [(注入前, 注入后)], 预期变红的用例, 是否反向)
# 反向校验点（reverse=True）要求判定为"未抓到"：用来证明判定助手没被
# 环境故障骗过——注入与守卫无关时，任何点名都不该被满足。
ANCHORS: list[tuple[str, str, list[tuple[str, str]], list[str], bool]] = [
    ("A", "预检方法不存在（基础处理器一律回 501）", [
        ("    def do_OPTIONS(self) -> None:    # noqa: N802",
         "    def do_OPTIONS_disabled(self) -> None:    # noqa: N802"),
        # test_4 一并红：预检整体 501 时，无来源的调用方同样拿不到 204。
    ], [case(_P, "test_1_tauri_production_shell_preflight_ok"),
        case(_P, "test_2_vite_dev_origin_preflight_ok"),
        case(_P, "test_3_untrusted_origin_preflight_rejected"),
        case(_P, "test_4_non_browser_client_no_origin"),
        case(_P, "test_5_preflight_then_real_post_succeeds")], False),

    ("B", "长度校验挪回建会话之后（被拒却留下空会话）", [
        (_CHECK + _MAKE, _MAKE + _CHECK),
    ], [case(_Q, "test_7_overlong_message_creates_no_conversation"),
        case(_Q, "test_8_overlong_message_stream_path_same")], False),

    # -------- 防修过头方向 --------
    ("C", "来源一律回显（恶意来源也拿到放行头）", [
        ("        return origin if _origin_host(origin) in "
         "_allowed_origin_hosts() else None",
         "        return origin"),
    ], [case(_P, "test_3_untrusted_origin_preflight_rejected")], False),

    ("D", "预检要求必须带来源（非浏览器客户端全被拒）", [
        ("        if not self._origin_allowed():\n"
         "            self._json({\"error\": \"不允许的访问来源\",\n"
         "                        \"code\": CODE_FORBIDDEN}, 403)\n"
         "            return\n"
         "        self.send_response(204)",
         "        if not (self.headers.get(\"Origin\", \"\")\n"
         "                and self._origin_allowed()):\n"
         "            self._json({\"error\": \"不允许的访问来源\",\n"
         "                        \"code\": CODE_FORBIDDEN}, 403)\n"
         "            return\n"
         "        self.send_response(204)"),
    ], [case(_P, "test_4_non_browser_client_no_origin")], False),

    ("E", "声明的动词超出实际支持（写了 PUT/DELETE）", [
        ('"GET, POST, OPTIONS")', '"GET, POST, PUT, DELETE, OPTIONS")'),
    ], [case(_P, "test_6_methods_declared_minimal")], False),

    # -------- 判定助手自身的自检 --------
    ("X", "无关故障不得被当成抓到（模块级未定义名）", [
        # 锚点必须落在模块级代码上。插进文档字符串**内部**的话那行只是字符串
        # 内容、不是代码，用例照常全绿——此时反向校验点的"未抓到"不是判定助手
        # 识破了环境故障，而是压根没注入成功，校验点恒假。
        ("import threading\n", "import threading\n_unrelated_undefined_name_\n"),
    ], [], True),
]


def main() -> int:
    only = set(sys.argv[1:])
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    orig = SRC.read_text(encoding="utf-8")

    caught = 0
    ran = 0
    for name, desc, subs, expect, reverse in ANCHORS:
        if only and name not in only:
            continue
        ran += 1
        cur = orig
        hit = True
        for old, new in subs:
            if old not in cur:
                hit = False
                break
            cur = cur.replace(old, new, 1)
        if not hit:
            print(f"  [未抓到] {name} {desc} —— 注入点不存在，锚点须重定位")
            continue
        # 备份必须在注入之前写：若备份晚于注入，被中断后备份里存的是已注入
        # 版本，还原回到的是脏代码，而此后所有校验点都在这份脏代码上跑。
        BACKUP.write_text(orig, encoding="utf-8")
        # 登记必须在改源码之前：中断后靠它把源码对齐回去
        reg_add(SRC, BACKUP)
        SRC.write_text(cur, encoding="utf-8")
        try:
            rc, failed, tail = pytest_run(TESTS)
        finally:
            restore_src(SRC, BACKUP)
        if reverse and rc == 0:
            # 反向校验点要靠"注入造成故障"才有意义。注入落在注释或文档字符串
            # 里时用例照常全绿，判定助手同样给出"未抓到"——那不是识破了环境
            # 故障，而是校验点压根没生效，于是恒真。
            ok = False
            why = "注入未造成任何故障（用例全绿）——反向校验点失去意义"
        else:
            got, why = verdict(rc, failed, expect)
            ok = (not got) if reverse else got
        caught += int(ok)
        label = "抓到" if ok else "未抓到"
        print(f"  [{label}] {name} {desc}: {why}")
        if not ok and reverse:
            print("        反向校验点要求判定为未抓到，实际判成抓到")
    print(f"\n{caught}/{ran} 抓到")

    rc, failed, tail = pytest_run(TESTS)
    print(f"  基线：{tail}")
    if rc != 0:
        print(f"  基线不通过：{failed[:5]}")
        return 1
    return 0 if caught == ran else 1


if __name__ == "__main__":
    sys.exit(main())
