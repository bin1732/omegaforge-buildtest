#!/usr/bin/env python3
"""L0 归一化与注入扫描守卫的判定校验。

## 为什么补这个脚本

`tests/test_provenance_contract.py` 的说明里写着"每条断言都做过判定校验"，
但仓库里没有脚本引用过它。这类声称无法被复查：代码漂移后守卫可能已经
静默失效，而全量回归通过看不出区别——恒真的守卫同样是绿的。本脚本把声称
换成可执行、可重跑的证据。

## 两个锚点对应两种失效形态，互不覆盖

 * A 归一化恒返回原文：安全判断退回原始字节。症状不是报错，而是"底线悄悄
   失效"——`rm<U+200B> -rf /` 渲染出来是 `rm -rf /`，判定却不再拦。危害
   不依赖命令真能执行：审批欺骗（用户批准的是他看到的那条）与审计失真
   （审计链记下变形文本，事后取证对不上）已经成立。
 * B 注入扫描恒返回空：可疑句式一条都标不出来，外部内容不再降级为"被引用
   的数据"。

A 只让 11 条变红，B 只让 8 条变红，且两者只在一个参数化实例上重叠。因此
只验 A 时，B 这条路径被摘掉不会有任何校验点变红。

点名清单取自实际执行结果，判定用严格模式：出现预期之外的失败项同样判未
抓到——整片都红的那种红是环境故障，不是守卫在起作用。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_provenance_contract.py"
C = TESTS + "::"

NORM = ROOT / "omegaforge" / "tools" / "normalize.py"
PROV = ROOT / "omegaforge" / "tools" / "provenance.py"

ANCHORS = [
    ("A 归一化恒返回原文", NORM,
     '    s = str(text or "")\n'
     '    # 1) 兼容折叠：全角 ASCII、数学字母、上标等\n'
     '    s = unicodedata.normalize("NFKC", s)\n'
     '    # 2) 剥离不可见字符\n'
     '    s = _INVISIBLE_RE.sub("", s)\n'
     '    # 3) 同形字按形状折叠\n'
     '    s = _HOMOGLYPH_RE.sub(lambda m: _HOMOGLYPHS[m.group(0)], s)\n'
     '    return s',
     '    return str(text or "")',
     [C + "test_invisible_chars_are_stripped",
      C + "test_homoglyphs_fold_to_latin",
      C + "test_dangerous_cmd_not_bypassable_by_invisible",
      C + "test_protected_path_not_bypassable",
      C + "test_internal_host_not_bypassable",
      C + "test_injection_detected"]),
    ("B 注入扫描恒返回空", PROV,
     '    norm = normalize(text)\n'
     '    hits = []\n'
     '    for tag, rx in _COMPILED:\n'
     '        if rx.search(norm) and tag not in hits:\n'
     '            hits.append(tag)\n'
     '    return hits\n'
     '\n'
     '\n'
     'def wrap(',
     '    return []\n'
     '\n'
     '\n'
     'def wrap(',
     [C + "test_injection_detected",
      C + "test_wrap_has_boundary_and_source",
      C + "test_fs_read_carries_provenance"]),
    ("C 基线", None, None, None, []),
]


def _restore(target: Path, backup: Path):
    shutil.move(str(backup), str(target))


def self_heal():
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。备份与被注入文件同目录，不放在 /tmp：后者在
    命令之间会被回收，备份没了就无法还原。
    """
    for p in (NORM, PROV):
        bak = Path(str(p) + ".bak")
        if bak.exists():
            _restore(p, bak)
            print(f"  [自愈] 还原 {p.name}")


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
