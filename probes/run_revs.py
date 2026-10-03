#!/usr/bin/env python3
"""统一执行各回退校验脚本，并先把环境补齐。

## 环境为什么要在这里补齐

这些脚本大多靠子进程跑用例。依赖装在非标准位置时，模块不在默认搜索
路径上，子进程直接报 ModuleNotFoundError。后果不只是"跑不了"：

 · 有的脚本把 rc≠0 记成"变更被抓到"——环境缺失会被读成产品回归
 · 有的脚本先跑基线，基线起不来就报"基线就不干净"，排查方向被带偏

逐个改脚本里的 env 既不现实也容易漏。子进程继承父进程环境，所以只需
要在调用方补齐一次。

## 另一半作用：给出一条命令

没有统一入口时，这些脚本只在需要时手动跑单个，坏掉多久都不会被发现。
本入口第一次跑就查出一个脚本的注入锚点早已失效（脚本崩溃退出，三个
校验点里有两个从未真正执行过）。

## 用法

    python3 probes/run_revs.py --fast     # 只跑后端类（不需要浏览器）
    python3 probes/run_revs.py --all      # 含前端类（需要浏览器与构建产物）
    python3 probes/run_revs.py rev_cors   # 只跑指定几个（可省略 probes/ 与 .py）

退出码：0=全部通过，1=有脚本未通过（或 --require-all 下有脚本未跑），
2=还原异常。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBES = ROOT / "probes"
# 与执行器保持一致：只在这几棵目录里认校验产物的残留
GUARDED = ["omegaforge", "tests", "frontend/src"]

# 非标准位置的依赖目录：依赖装在这里时，不补进 PYTHONPATH 子进程就找不到
EXTRA_SITES = ["/data/workspace/.pypkgs", "/data/workspace/pypkgs"]

# 需要真浏览器或前端构建产物，单轮跑不动，默认排除
SLOW = {
    "rev_backend_down", "rev_first_run", "rev_frontend_audits",
    "rev_frontend_interaction", "rev_frontend_render",
}


def check_slow(scripts: list[Path], which: str) -> list[str]:
    """SLOW 里的名字必须真的存在。

    幽灵条目（名字对不上任何脚本）意味着"排除"从未生效：本该被排除的
    脚本仍在 fast 里跑，而它一旦改名就会以超时形式冒出来，看起来像脚本
    自身有问题。这类沉默的错位要主动报出来。
    """
    if which != "fast":
        return []
    have = {p.stem for p in scripts}
    ghost = sorted(n for n in SLOW if n not in have)
    return ghost


def preflight(env: dict) -> None:
    """先确认子进程真能跑起用例。

    缺件时脚本会把环境故障记成"基线就不干净"，读起来像产品回归。与其
    等脚本给出误导性的结论，不如在开工前把缺件说清楚。
    """
    p = subprocess.run(
        [sys.executable, "-c",
         "import pytest, pytest_timeout; print(pytest.__version__)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        raise SystemExit(
            "环境缺件：pytest 或 pytest-timeout 不可用\n  "
            + "\n  ".join(tail[-3:])
            + "\n  可装到 /data/workspace/.pypkgs："
              "pip install pytest pytest-timeout --target /data/workspace/.pypkgs")
    print(f"环境自检通过：pytest {p.stdout.strip()}")


def build_env() -> dict:
    env = dict(os.environ)
    parts = [p for p in (env.get("PYTHONPATH", ""), str(ROOT)) if p]
    for site in EXTRA_SITES:
        if Path(site).is_dir():
            parts.append(site)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


def list_scripts(which: str) -> list[Path]:
    if which == "names":
        return []
    all_scripts = sorted(PROBES.glob("rev_*.py"))
    if which == "fast":
        return [p for p in all_scripts if p.stem not in SLOW]
    return all_scripts


def summarize(log: str) -> str:
    """取最能说明结果的一行；没有标志性输出时给个兜底。"""
    for line in reversed(log.splitlines()):
        s = line.strip()
        if not s:
            continue
        for key in ("锚点抓到", "精确抓到", "全部抓到", "未抓到", "结论",
                    "基线", "还原校验", "rc="):
            if key in s:
                return s[:110]
    return (log.strip().splitlines() or ["(无输出)"])[-1][:110]


def sweep_untracked(rels: list[str]) -> list[str]:
    """清掉未跟踪的残留，只认校验产物的命名。

    未跟踪不等于可删：工作区里也可能放着临时脚本。因此只删受守护目录
    内、以 .bak 结尾或命中样本命名模式的文件。
    """
    pats = ("*.bak", "_rev_scope_probe_*.py")
    keep = []
    for rel in rels:
        if rel.startswith(tuple(GUARDED)) and any(
                fnmatch(rel, p) or fnmatch(Path(rel).name, p) for p in pats):
            keep.append(rel)
    swept = []
    for rel in keep:
        try:
            (ROOT / rel).unlink()
            swept.append(rel)
        except OSError as e:
            print(f"  [run_revs] 残留清理失败 {rel}：{e}")
    return swept


def run_one(script: Path, timeout: int, env: dict) -> tuple[int, str]:
    runner = PROBES / "rev_runner.py"
    cmd = [sys.executable, str(runner), str(script), str(timeout)]

    def _go():
        return subprocess.run(cmd, cwd=str(ROOT), env=env,
                              capture_output=True, text=True,
                              timeout=timeout + 120)

    p = _go()
    # rc=2 是执行器"执行前就不干净"的拒绝码。它偶发出现在上一个脚本刚
    # 还原之后——文件系统尚未落定，git 仍报脏。直接记成失败会让人以为是
    # 脚本本身有问题，所以先确认一次：确实脏就说清是哪些文件，不脏就重跑。
    if p.returncode == 2:
        time.sleep(5)
        # 复查的口径必须与执行器一致（都只看受守护目录）。用全仓口径会让
        # probes/ 下的编辑把结论顶成"未执行"——而执行器其实放行并跑完了，
        # 于是明明有结果却报成环境脏。
        chk = subprocess.run(["git", "status", "--porcelain", "--", *GUARDED],
                             cwd=str(ROOT), capture_output=True, text=True)
        dirty = [l for l in (chk.stdout or "").splitlines() if l.strip()]
        if not dirty:
            p = _go()
        else:
            # 脏分两种，处置不同：未跟踪的残留不属于任何源码版本，清掉
            # 不改变内容；已跟踪文件被改动则可能是编写中的内容，一律用
            # HEAD 覆盖会把那次改动一起抹掉。所以只自动处理前一种。
            untracked = [l[3:].strip() for l in dirty if l.startswith("??")]
            tracked = [l for l in dirty if not l.startswith("??")]
            if untracked and not tracked:
                swept = sweep_untracked(untracked)
                if swept:
                    print(f"  [run_revs] 清掉上一轮留下的残留后重试："
                          f"{', '.join(swept)}")
                    p = _go()
                    if p.returncode != 2:
                        return p.returncode, (p.stdout or "") + (p.stderr or "")
            note = "\n".join(dirty[:8])
            return 2, (f"{p.stdout}{p.stderr}\n"
                       f"[run_revs] 工作区确实不干净，未执行：\n{note}")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="指定脚本名，可省略 probes/ 与 .py")
    ap.add_argument("--all", action="store_true", help="含前端类脚本")
    ap.add_argument("--fast", action="store_true", help="只跑后端类（默认）")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--require-all", action="store_true",
                    help="有脚本未跑也判未通过（防把缺席当成通过）")
    args = ap.parse_args()

    if args.names:
        scripts = []
        for n in args.names:
            stem = n[:-3] if n.endswith(".py") else n
            path = PROBES / f"{stem}.py"
            if not path.exists():
                print(f"找不到脚本：{path}")
                return 1
            scripts.append(path)
    else:
        scripts = list_scripts("all" if args.all else "fast")

    env = build_env()
    preflight(env)
    print(f"PYTHONPATH 已补齐：{env['PYTHONPATH']}")

    all_names = sorted(p.stem for p in PROBES.glob("rev_*.py")
                       if p.stem != "rev_runner")
    # 指定脚本时同样要算未跑的：只报"跑过的通过了多少"，会让人把一次
    # 抽查读成全量通过——这正是本入口要防的那类误读。
    skipped = sorted(set(all_names) - {p.stem for p in scripts})
    for g in check_slow(list(PROBES.glob("rev_*.py")),
                        "all" if args.all else "fast"):
        print(f"[提示] SLOW 里的 {g} 对不上任何脚本，排除从未生效")

    print(f"待跑 {len(scripts)} 个 / 共 {len(all_names)} 个，"
          f"单个超时 {args.timeout} 秒")
    # 未跑的必须显式列出：只报"跑过的通过了多少"会让人把部分通过读成全过。
    if skipped:
        print(f"未跑 {len(skipped)} 个（需真浏览器或前端产物，"
              f"加 --all 才跑）：{', '.join(skipped)}")
    print()

    bad = []
    # rc=2 是执行器"执行前就不干净"的拒绝码：脚本一条没跑，与"跑了但
    # 没抓到"是两件事。混进同一份未通过清单，会让一次环境脏读成一整批
    # 校验失效，排查方向被引到那批脚本上。
    unexecuted = []
    for s in scripts:
        try:
            rc, log = run_one(s, args.timeout, env)
        except subprocess.TimeoutExpired:
            # 超时常被读成"脚本坏了"，但更可能是它本该划进 SLOW
            bad.append((s.stem, f"超时 {args.timeout} 秒"
                        + ("（若它需要浏览器或前端产物，应加入 SLOW）"
                           if s.stem not in SLOW else ""), -1))
            print(f"  [超时] {s.stem}")
            continue
        tail = summarize(log)
        if rc == 2:
            unexecuted.append((s.stem, tail))
            print(f"  [未执行] {s.stem:<28} rc=2  工作区不干净，脚本未启动")
        elif rc != 0:
            bad.append((s.stem, tail, rc))
            print(f"  [未通过] {s.stem:<28} rc={rc}  {tail}")
        else:
            print(f"  [通过]   {s.stem:<28} rc=0  {tail}")
        sys.stdout.flush()

    print("\n" + "=" * 72)
    print(f"本次跑 {len(scripts)} 个，通过 {len(scripts) - len(bad) - len(unexecuted)} 个"
          + f"，未通过 {len(bad)} 个，未执行 {len(unexecuted)} 个"
          + (f"；另有 {len(skipped)} 个未跑，全量需 --all"
             if skipped else "；全量已覆盖"))
    for name, tail, rc in bad:
        print(f"  未通过 {name} (rc={rc})：{tail}")
    if unexecuted:
        print(f"  未执行 {len(unexecuted)} 个（工作区脏，结论为未知而非失败）：")
        for name, tail in unexecuted:
            print(f"    {name}：{tail[:100]}")
        # 脏会挡住此后每一个校验，而脚本不启动就没有自愈的机会——这是
        # 死锁，必须给出解锁动作。
        print("  解锁：确认这些改动是上一轮校验的残留后，用 "
              "git checkout -- <文件> 恢复再重跑")
    # 有人只想看"有没有问题"，但"没跑"不等于"没问题"。--require-all 用于
    # 要求覆盖完整，否则把缺席当成通过。
    if args.require_all and skipped:
        print(f"  未跑（--require-all 视为未通过）：{', '.join(skipped)}")
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
