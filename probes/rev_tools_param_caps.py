#!/usr/bin/env python3
"""工具参数上限守卫的判定校验：撤掉修复，指定的用例必须变红。

## 为什么要点名到用例

只比对"有没有失败项"区分不了两件事：失败的是不是这一处守卫对应的那条
用例。给被测模块加一个模块级未定义名，整片用例都会失败，任何"有失败"
的判定都会被满足——那时的红是环境故障，不是守卫在起作用。

因此每个校验点都点名预期变红的用例，并且严格：出现预期之外的失败项同样
判未抓到。

## 两类校验点缺一不可

A~E 是"撤掉修复"方向，证明修复有用；F、G 是"防修过头"方向，证明修复刚好
够用。只留前一类的话，把上限收紧成一律拒绝同样报全绿——拦截确实发生了，
只是正常用法一起被拒。这类失效不会被任何"撤掉"方向的校验点发现。

## 点名的来源

每个点名清单都要逐个注入确认。照着注入点推出来的清单会漏掉连带失败的
那几条，而漏掉的后果是：注入生效了，判定却说"预期之外还失败"。

用法::

    python3 probes/rev_tools_param_caps.py          # 全部校验点
    python3 probes/rev_tools_param_caps.py A B      # 只跑指定校验点
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT", "/data/workspace/LATEST"))
sys.path.insert(0, str(ROOT / "probes"))

from _rev_verdict import pytest_run, reg_add, restore_src, verdict  # noqa: E402

SRC = ROOT / "omegaforge" / "tools" / "system_tools.py"
TESTS = "tests/test_tools_param_caps.py"
BACKUP = Path("/data/workspace/.rev_backups/system_tools.py.bak")


def case(kls: str, name: str) -> str:
    return f"{TESTS}::{kls}::{name}"


_R = "FsReadCapTest"
_W = "FsWriteCapTest"
_C = "CommandOutputCapTest"
_D = "WebFetchRedirectTest"
_E = "WebFetchCapTest"

# (标识, 说明, [(注入前, 注入后)], 预期变红的用例, 是否反向)
# 反向校验点（reverse=True）要求判定为"未抓到"：用来证明判定助手没被
# 环境故障骗过——注入与守卫无关时，任何点名都不该被满足。
ANCHORS: list[tuple[str, str, list[tuple[str, str]], list[str], bool]] = [
    ("A", "fs_read 体积收敛（_as_bytes + 按需读）", [
        ("        max_bytes = _as_bytes(max_bytes, FS_READ_DEFAULT, FS_READ_CAP)\n",
         ""),
        ("        with open(p, \"rb\") as f:\n            data = f.read(max_bytes)\n",
         "        data = p.read_bytes()[:max_bytes]\n"),
    ], [case(_R, "test_1_read_volume_is_bounded_by_max_bytes"),
        case(_R, "test_2_huge_max_bytes_is_capped"),
        case(_R, "test_3_negative_and_none_fall_back_to_default"),
        case(_R, "test_4_non_integer_does_not_raise")], False),

    ("B", "fs_write 体量上限", [
        ("        n = len((content or \"\").encode(\"utf-8\"))\n"
         "        if n > FS_WRITE_CAP:\n"
         "            raise UserError(\n"
         "                f\"单次写入上限 {FS_WRITE_CAP // 1024} KB，\"\n"
         "                f\"本次 {n // 1024} KB，请拆分后再写入\")\n",
         ""),
    ], [case(_W, "test_1_oversized_write_is_rejected"),
        case(_W, "test_2_message_reaches_the_surface")], False),

    ("C", "run_command 输出不进内存", [
        ("            with tempfile.TemporaryFile() as buf:\n"
         "                r = subprocess.run(cmd, shell=True, stdout=buf, stderr=buf,\n"
         "                                   timeout=timeout, cwd=str(self.home))\n"
         "                total = buf.seek(0, os.SEEK_END)\n"
         "                buf.seek(0)\n"
         "                out = buf.read(CMD_OUTPUT_CAP).decode(\"utf-8\", errors=\"replace\")\n",
         "            r = subprocess.run(cmd, shell=True, capture_output=True,\n"
         "                               text=True, timeout=timeout, cwd=str(self.home))\n"
         "            out = (r.stdout or \"\") + ((\"\\n[stderr] \" + r.stderr) if r.stderr else \"\")\n"
         "            total = len(out)\n"),
    ], [case(_C, "test_1_output_is_not_captured_into_memory")], False),

    ("D", "web_fetch 跳转重新过 SSRF 校验", [
        ("        opener = urllib.request.build_opener(_GuardedRedirectHandler)\n"
         "        with opener.open(req, timeout=20) as r:\n",
         "        with urllib.request.urlopen(req, timeout=20) as r:\n"),
    ], [case(_D, "test_1_redirect_to_loopback_is_blocked"),
        case(_D, "test_2_redirect_to_link_local_metadata_ip_is_blocked"),
        case(_D, "test_3_non_http_redirect_is_blocked"),
        case(_D, "test_4_redirect_loop_is_bounded")], False),

    ("E", "web_fetch 体积参数收敛", [
        ("        max_bytes = _as_bytes(max_bytes, WEB_FETCH_DEFAULT, WEB_FETCH_CAP)\n",
         ""),
    ], [case(_E, "test_1_none_falls_back_to_default"),
        case(_E, "test_2_negative_falls_back_to_default"),
        case(_E, "test_3_oversized_is_capped"),
        case(_E, "test_4_non_integer_does_not_raise")], False),

    # -------- 防修过头方向 --------
    ("F", "跳转被一律拒绝（正常跳转也走不通）", [
        ("        if hops > REDIRECT_MAX:", "        if hops > 0:"),
    ], [case(_D, "test_1_redirect_to_loopback_is_blocked"),
        case(_D, "test_2_redirect_to_link_local_metadata_ip_is_blocked"),
        case(_D, "test_3_non_http_redirect_is_blocked"),
        case(_D, "test_5_normal_redirect_still_works")], False),

    ("G", "写入被一律拒绝（上限内的正常写入也被拒）", [
        ("        if n > FS_WRITE_CAP:", "        if n > 0:"),
    ], [case(_W, "test_3_at_cap_is_allowed"),
        case(_W, "test_4_normal_write_unaffected")], False),

    # -------- 判定助手自身的自检 --------
    ("X", "无关故障不得被当成抓到（模块级未定义名）", [
        ("from __future__ import annotations\n",
         "from __future__ import annotations\n_unrelated_undefined_name_\n"),
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
        got, why = verdict(rc, failed, expect)
        ok = (not got) if reverse else got
        caught += int(ok)
        label = "抓到" if ok else "未抓到"
        print(f"  [{label}] {name} {desc}: {why}")
        if not ok and reverse:
            print(f"        反向校验点要求判定为未抓到，实际判成抓到")
    print(f"\n{running_summary(ran, caught)}")

    rc, failed, tail = pytest_run(TESTS)
    print(f"  基线：{tail}")
    if rc != 0:
        print(f"  基线不通过：{failed[:5]}")
        return 1
    return 0 if caught == ran else 1


def running_summary(ran: int, caught: int) -> str:
    return f"{caught}/{ran} 抓到"


if __name__ == "__main__":
    sys.exit(main())
