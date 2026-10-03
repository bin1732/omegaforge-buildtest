#!/usr/bin/env python3
"""相关部分校验：确认全端点联调守卫不是空转。

每个校验点注入后必须：
  1. 确认替换真的发生（字符串计数变化）
  2. .py 文件强制 ast.parse 通过（否则"测试没跑起来"会被误读成"抓到"）
  3. 期望有真实 failed 数
  4. 还原，并跑一次对照（还原后必须全绿）
"""
import ast
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST = "tests/test_e2e_full_contract.py"
SERVER = "omegaforge/server.py"
APITS = "frontend/src/lib/api.ts"
KPAGE = "frontend/src/pages/KnowledgePage.tsx"

# EXPECT：显式声明每个校验点的期望。不追求"全部抓到"的漂亮结果——
# 未抓到的必须说清为什么，不能藏起来，也不能凑成全绿。
EXPECT = {
    "A 撤后端 wiki/page 的 name 兼容": "抓到",
    "B 前端 wikiPage 退回 ?name=": "抓到",
    "C 撤解析器窗口截断": "抓到",
    "D 撤后端 need 窗口截断": "抓到",
    "E 前端 kbAdd 漏发 title（后端必需）": "抓到",
}

# 关于 C：这里更正过一次结论。
#
# 上一版把 C 记为"未抓到"，依据是脚本输出 SKIP（旧串命中 0 次）。但 SKIP
# 的真正原因是：当时版本库里停着一份**被注入的**待测文件（窗口截断已被
# 写成 nxt = -1），于是校验点的 old 串根本不存在，校验点从未执行。我把
# "校验点没跑"读成了"校验点跑了但没抓到"，据此得出"窗口截断已降级为防御性
# 冗余"——这是假结论，已随注入一起被提交进版本库。
#
# 真实实现恢复后重跑：撤掉窗口截断 → 36 个前端函数全部解析串台
# （fetchConversations 是 GET，却解析出 /api/conversations/new），
# 2 项变红。窗口截断是必需的，不是冗余。
#
# 教训：校验脚本遇到"旧串命中 0 次"必须直接判失败，不能当 SKIP
# 跳过——否则注入态、版本库漂移都会被静默吞掉。

C = TEST + "::"

# 每个校验点都点名应当变红的用例：只看失败条数说明不了失败的是不是预期
# 那一条。
ANCHORS = [
    ("A 撤后端 wiki/page 的 name 兼容", SERVER,
     'slug = (qs.get("slug") or qs.get("name") or [""])[0]',
     'slug = (qs.get("slug") or [""])[0]',
     [C + "test_wiki_page_legacy_name_still_works"]),
    ("B 前端 wikiPage 退回 ?name=", APITS,
     "?slug=${encodeURIComponent(slug)}",
     "?name=${encodeURIComponent(slug)}",
     [C + "test_wiki_page_uses_slug"]),
    ("C 撤解析器窗口截断", TEST,
     'nxt = src.find("\\nexport ", start + 1)',
     "nxt = -1",
     [C + "test_get_functions_parse_as_no_post",
      C + "test_window_never_overruns_into_next_export"]),
    # D 的注入点在测试文件里，而该文件用 4 空格缩进（不是 8），且这个窗口
    # 截断模式在文件里出现两次（be_required 与 be_query_keys）。缩进写错或
    # 只取前两行都会让锚点命中 0 次——锚点失效会被读成"未抓到"，与"抓到"
    # 无从区分。因此锚点串必须连带下一行的 mn = re.search 才唯一。
    ("D 撤后端 need 窗口截断", TEST,
     '    if nxt:\n      w = w[:10 + nxt.start()]\n    mn = re.search(r"\\bneed\\(([^)]*)\\)", w, re.S)',
     '    if False:\n      w = w[:10 + nxt.start()]\n    mn = re.search(r"\\bneed\\(([^)]*)\\)", w, re.S)',
     [C + "test_backend_required_not_confused_across_endpoints"]),
    ("E 前端 kbAdd 漏发 title（后端必需）", KPAGE,
     "kbAdd({ title: title.trim(), text: text.trim() })",
     "kbAdd({ text: text.trim() })",
     [C + "test_kb_add_not_confused_with_kb_search",
      C + "test_required_fields_present",
      C + "test_endpoints_accept"]),
]


def run():
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    判定必须点名：只看失败条数说明不了失败的是不是预期那一条——注入一个
    无关的模块级错误能让整片用例全红，此时任何"有失败"的判定都会被满足。
    """
    import sys as _s
    if REPO not in _s.path:
        _s.path.insert(0, REPO)
    from probes._rev_verdict import pytest_run
    return pytest_run(TEST)


def main():
    # 可选：只跑名字含给定关键字的校验点。整脚本连跑六次 pytest 会超出
    # 单次命令时限，被中断后源文件可能停在"已注入"状态；分批跑能把
    # 单次耗时压下来，同时由 rev_runner 的守护逻辑兜住残留。
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    results = []
    for name, f, old, new, expect in ANCHORS:
        if only and only not in name:
            continue
        path = os.path.join(REPO, f)
        src = open(path, encoding="utf-8").read()
        if src.count(old) != 1:
            # 命中 0 次 = 校验点失效（须重定位）；命中多次 = 注入位置不确定。
            #
            # 两种情况都必须判**失败**，不能当 SKIP 跳过：上一版正是把
            # "旧串命中 0 次"读成"未抓到"，让一份被注入的待测文件混进
            # 版本库，还据此得出了一条假结论。校验点没跑 ≠ 校验点跑了没抓到。
            results.append((name, f"锚点失效-命中{src.count(old)}次 ←不符",
                            -1))
            print(f"  {name:38s} ✗ 锚点失效（旧串命中 {src.count(old)} 次）")
            continue
        open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
        try:
            if f.endswith(".py"):
                try:
                    ast.parse(open(path, encoding="utf-8").read())
                except SyntaxError as e:
                    results.append((name, "注入后语法错误 ←不符", -1))
                    print(f"  {name:38s} 注入后语法错误：{e}")
                    continue
            rc, failed, tail = run()
        finally:
            # 还原必须无条件执行：上一版把它写在 try 之后，中途 continue
            # 或异常时源文件就停在"被注入"的状态，检验留下过被改坏的
            # 前端页面文件。
            open(path, "w", encoding="utf-8").write(src)
        import sys as _s2
        if REPO not in _s2.path:
            _s2.path.insert(0, REPO)
        from probes._rev_verdict import verdict as _verdict
        ok, why = _verdict(rc, failed, expect)
        got = "抓到" if ok else "未抓到"
        exp = EXPECT.get(name, "抓到")
        ok = (got == exp)
        results.append((name, f"{got}/期望{exp}（{why}）{'' if ok else ' ←不符'}",
                        len(failed)))
        print(f"  {name:38s} {got}（期望{exp}）{'' if ok else '  ←不符'} "
              f"{why}")

    print("\n=== 对照：全部还原后必须全绿 ===")
    rc_b, failed_b, tail = run()
    nfail = len(failed_b)
    print(f"  还原后 rc={rc_b} failed={nfail}  ({tail.strip()[:80]})")
    print("\n=== 汇总 ===")
    print(json.dumps([{"锚点": n, "结果": r, "failed": f}
                      for n, r, f in results], ensure_ascii=False, indent=1))
    bad = [n for n, r, f in results if "←不符" in r]
    print("未抓到/异常锚点：", bad if bad else "无")
    # 退出码必须真实反映结果：main() 不给返回值时脚本恒以 0 退出，
    # bad 算了也不影响退出码——"零个校验点执行过"会被读成"验过且通过"。
    ok = not bad and nfail == 0
    print("结论：", "全部按预期" if ok else f"{len(bad)} 项不符")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
