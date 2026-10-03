#!/usr/bin/env python3
"""系统工具安全守卫（危险命令 / 工作区路径 / SSRF）的判定校验。

## 为什么补这个脚本

`tests/test_system_tools_security.py`（33 项）覆盖三类可复现缺陷——危险命令
黑名单失效、工作区路径前缀校验可绕过、web_fetch 无内网防护。文件说明里逐条
写了复现方式，但仓库里没有脚本引用过它：代码漂移后守卫可能已经静默失效，
而全量回归通过看不出区别。本脚本把"能复现"换成可执行、可重跑的证据。

## 四个锚点对应四条互不覆盖的路径

 * A 整词 + 字面子串判定：撤掉后 10 条变红，其中 `rm -rf /` 是历史上真正
   漏掉的那条（原正则在 `rm -rf /` 这类以非单词字符结尾的分支后加了 `\b`，
   行尾不成立，于是最危险的命令反而不拦）。
 * B 结构判定：撤掉后只有 `su -` 变红。覆盖的是 A 够不到的分支——两者都不
   撤，清单才是完整的；只验 A 时 B 被摘掉不会有任何校验点变红。
 * C 工作区路径：撤掉后只有 `../.omegaforge_evil/secret.txt` 变红。正是
   字符串前缀校验的绕过样本——同级目录同样以 `.omegaforge` 开头。
 * D 内网地址防护：撤掉后 8 条变红（含 `169.254.169.254` 云元数据）。
   SSRF 与命令黑名单是两个独立的面，互相证不了对方。

点名清单取自实际执行结果，判定用严格模式：出现预期之外的失败项同样判未抓到
——整片都红的那种红是环境故障，不是守卫在起作用。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_system_tools_security.py"
C = TESTS + "::"
ST = ROOT / "omegaforge" / "tools" / "system_tools.py"

ANCHORS = [
    ("A 撤掉整词与字面子串判定", ST,
     "    if _CMD_BLOCK.search(norm):\n"
     "        return True\n"
     "    low = norm.lower()\n"
     "    if any(bad in low for bad in _CMD_DANGER_SUBSTR):\n"
     "        return True\n",
     "",
     [C + "test_dangerous_commands_blocked"]),
    ("B 撤掉结构判定", ST,
     "    return _structural_dangerous(norm)",
     "    return False",
     [C + "test_dangerous_commands_blocked"]),
    ("C 路径校验退回字符串前缀", ST,
     "        try:\n            p.relative_to(root)\n        except ValueError:",
     "        try:\n"
     "            if not str(p).startswith(str(root)):\n"
     '                raise ValueError("x")\n'
     "        except ValueError:",
     [C + "test_path_escape_blocked"]),
    ("D 撤掉内网地址防护", ST,
     '    host = (urlparse(url).hostname or "").strip("[]").lower()\n'
     "    if not host:\n"
     '        raise ValueError("invalid url host")',
     "    return",
     [C + "test_internal_url_blocked",
      C + "test_non_http_scheme_rejected"]),
    ("E 基线", None, None, None, []),
]


def _restore(target: Path, backup: Path):
    shutil.move(str(backup), str(target))


def self_heal():
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。备份与被注入文件同目录，不放在 /tmp：后者在
    命令之间会被回收，备份没了就无法还原。
    """
    bak = Path(str(ST) + ".bak")
    if bak.exists():
        _restore(ST, bak)
        print(f"  [自愈] 还原 {ST.name}")


def main() -> int:
    self_heal()
    bad = []
    for name, target, old, new, expect in ANCHORS:
        if target is None:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        src = target.read_text(encoding="utf-8")
        n = src.count(old)
        if n != 1:
            print(f"  [未抓到] {name}：锚点命中 {n} 次，须重定位")
            bad.append(name)
            continue
        bak = Path(str(target) + ".bak")
        shutil.copy2(target, bak)
        try:
            target.write_text(src.replace(old, new, 1), encoding="utf-8")
            if target.read_text(encoding="utf-8") == src:
                print(f"  [未抓到] {name}：写入没生效，注入未落盘")
                bad.append(name)
                continue
            rc, failed, tail = pytest_run(TESTS)
        finally:
            _restore(target, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:80]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    print(f"校验点 {len(ANCHORS)} 个，抓到 {len(ANCHORS) - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
