#!/usr/bin/env python3
"""前端十页真实渲染守卫的校验。

为什么必须做：这一章抓到的三个问题里，**有一个根本不是产品缺陷**——
radix-ui 用 CustomEvent 而测试环境没把它桥接到 jsdom，导致整页渲染崩溃。
这类基建缺陷最容易伪装成"产品崩了"，也最容易在下次改动中复发。校验
必须钉住它，不能只钉产品代码。

每个校验点声明"期望哪几条断言变红"，而不只是"有没有失败"：
  - C（日志退回破折号）必须**只**让日志断言红，"任务文本"仍绿。
    这正是假守卫被发现的机制：原先它通过只因为日志第一行恰好含任务文本。
    若 C 注入后任务断言也跟着红，说明又退回蹭同源文本的老路。

注入目标是**编译产物**（/data/workspace/ssrout/src/...），因为渲染守卫 require 的
就是它；这与"验证守卫能否抓到退化"的目标一致。每个校验点注入前备份、
验证后还原，最后强制校验回到全绿。
"""
import os
import re
import shutil
import subprocess
import sys

# 环境目录不能钉在 /tmp：沙盒的 /tmp 在调用之间会被回收，目录一消失整个
# 脚本就死了。默认值与渲染脚本保持一致（同是持久位置），避免被调用方
# 已改用持久路径、调用方仍指向临时目录这种分叉。
FE = os.environ.get("FE_HOME", "/data/workspace/fe_env")
OUT = os.environ.get("FE_OUT", "/data/workspace/ssrout") + "/src"
RENDER = f"{FE}/render_real_pages.cjs"
# 渲染检查脚本已固化进仓库；这里指向仓库副本，避免 /tmp 副本与仓库分叉
RENDER_REPO = "/data/workspace/LATEST/probes/frontend_render/render_real_pages.cjs"

TASK_A = "点开详情后渲染出真实任务文本"
LOG_A = "点开详情后渲染出真实日志文本（不是破折号）"
PROVIDER_A = "渲染出真实供应商名"

# 守卫的基建自检（事件类覆盖名单缺失）以退出码 3 报出，且发生在渲染之前，
# 因此它是一条**独立的失败形态**，不能与"页面崩溃"混为一谈。
SELFCHECK_TAG = "<基建自检>"
SELFCHECK_MARK = "渲染基建自检失败"


ANCHORS = [
    ("A 撤掉供应商中文名", {PROVIDER_A}, [
        (f"{OUT}/pages/SettingsPage.js",
         "children: labelOf(p) || id }", "children: String(p.name ?? id) }"),
    ]),
    ("B 撤掉 CustomEvent 强制覆盖", {SELFCHECK_TAG}, [
        (RENDER_REPO, "|CustomEvent|", "|__NEVER_MATCH__|"),
    ]),
    ("C 日志退回破折号", {LOG_A}, [
        (f"{OUT}/components/LogStream.js",
         'if (typeof l === "string")\n        return l.trim() || "—";',
         'if (typeof l === "string")\n        return "—";'),
    ]),
    ("D 撤掉详情的任务显示", {TASK_A}, [
        (f"{OUT}/pages/RunsPage.js",
         "taskOf(detail) && (", "false && ("),
    ]),
]


def run_render():
    p = subprocess.run(["node", RENDER_REPO], cwd=FE, capture_output=True,
                       text=True, timeout=600)
    out = p.stdout + p.stderr
    if SELFCHECK_MARK in out:
        return p.returncode, [SELFCHECK_TAG], out
    m = re.search(r"FAILED\((\d+)\): (.+)", out)
    if m:
        return p.returncode, [x.strip() for x in m.group(2).split("|")], out
    if "ALL PASS" in out:
        return p.returncode, [], out
    return p.returncode, ["<CRASH>"], out


def inject(pairs):
    for path, old, new in pairs:
        src = open(path, encoding="utf-8").read()
        if old not in src:
            raise SystemExit(f"注入失败：{path} 里找不到 {old!r}（守卫或产物已变）")
        shutil.copy2(path, path + ".bak")
        open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))


def restore(pairs):
    for path, _, _ in pairs:
        shutil.move(path + ".bak", path)


def self_heal():
    """把上一次运行遗留的注入痕迹清掉。

    本脚本若在注入与还原之间被中断（沙盒层面会直接掐断进程），产物与守卫
    会停在已注入状态，此后基线恒红、校验失去意义。因此每次开局先把
    所有校验点的注入态反向还原一遍，让本脚本幂等可重入。
    """
    for name, expect, pairs in ANCHORS:
        for path, old, new in pairs:
            try:
                src = open(path, encoding="utf-8").read()
            except FileNotFoundError:
                continue
            if new in src and old not in src:
                open(path, "w", encoding="utf-8").write(src.replace(new, old, 1))
                print(f"  [自愈] 还原上轮遗留注入：{name}")
            bak = path + ".bak"
            if os.path.exists(bak):
                os.remove(bak)


def main():
    # 每轮渲染要跑满十页 jsdom，耗时以分钟计；基线加若干锚点累计下来会超出
    # 单次调用上限而被掐断，中断若发生在注入与还原之间会留下已注入产物。
    # 支持单锚点入参，让每个校验点各自跑完、各自还原。
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    want = args[0].upper() if args else "all"
    # 单锚点模式跳过基线渲染：每个校验点都要求"期望红的全红且没有额外的红"，
    # 基线一旦不干净，多出来的红会让 got ⊆ want 不成立，同样判未抓到。
    skip_base = "--no-base" in sys.argv[1:] or (want != "all" and want != "BASE")
    self_heal()
    if skip_base:
        print("基线：跳过（单锚点模式；基线不干净会以'额外的红'形式被判未抓到）")
    else:
        rc0, f0, _ = run_render()
        print(f"基线：rc={rc0} 失败={f0}")
        if rc0 != 0:
            raise SystemExit("基线就不是全绿，反向验证没有意义")
        if want == "BASE":
            return 0
    if want != "all":
        picked = [a for a in ANCHORS if a[0].startswith(want)]
        if not picked:
            raise SystemExit(f"没有以 {want} 开头的校验点")
        ANCHORS[:] = picked

    bad = []
    for name, expect, pairs in ANCHORS:
        inject(pairs)
        try:
            rc, fails, out = run_render()
        finally:
            restore(pairs)
        got = set(fails)
        want = set(expect)
        # 精确匹配：期望红的全红，且没有额外的红（D 若连带日志红即为假守卫复发）
        ok = rc != 0 and want <= got and got <= want
        print(f"  [{'抓到' if ok else '未抓到'}] {name}")
        print(f"        期望红={sorted(want)}")
        print(f"        实际红={sorted(got)}")
        if not ok:
            bad.append(name)

    rc9, f9, _ = run_render()
    print(f"还原后：rc={rc9} 失败={f9}")
    if rc9 != 0:
        raise SystemExit("还原失败！")

    print()
    print("=== 汇总 ===")
    print(f"锚点 {len(ANCHORS)} 个，精确抓到 {len(ANCHORS) - len(bad)} 个")
    if bad:
        print("不合格：" + " | ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
