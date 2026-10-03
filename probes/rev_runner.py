#!/usr/bin/env python3
"""校验的安全执行器：无论脚本怎么结束，源码必须回到原样。

## 为什么需要这一层

校验的做法是"改源码 -> 跑测试 -> 还原"。还原写在脚本的
finally 里，看起来万无一失，但 finally 只在**脚本进程正常走到
出口**时才执行。执行被外部打断（沙盒回收、超时、502）时进程直接
消失，finally 来不及跑，源码就停在"被注入"的状态。

检验发生过一次：调度中断后 `omegaforge/core/run.py` 与
`omegaforge/server.py` 各留下一处注入，git 工作区看着像真实回归
——而那两个改动是我自己为了验证打进去的。这个状态极容易被误读成
"产品坏了"，进而去改本来没问题的代码。

## 做法

执行前先确认被测目录是干净的（否则拒绝执行，避免把别人的改动一起
抹掉）；执行后无条件用 git 把目录恢复到 HEAD。脚本自身崩了、超时、
被信号杀死，都不影响这一步。

## 用法

    python3 probes/rev_runner.py probes/rev_cors.py [超时秒数]

退出码沿用被驱动脚本的退出码；还原异常的退出码为 2。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 校验注入的不只是被测源码：前端页面与守卫用例同样会被临时改写
# （检验 rev_e2e_full_contract 的校验点就打在前端页面上，且脚本自身的
# 还原曾失效，留下被改坏的页面文件）。只护住一棵目录等于没护住。
GUARDED = ["omegaforge", "tests", "frontend/src"]

# 有的校验不改写已有文件，而是**新建**一个带问题的样本文件再让扫描去找
# 它。这类文件是新增的，git 恢复删不掉它——于是中断后它留在工作区，既
# 看着像真实改动，又会阻塞后续所有校验（本执行的"执行前必须干净"会
# 拒绝执行）。所以除了恢复受守护目录，还要按命名模式清掉这类残留。
LEFTOVER_GLOBS = ("_rev_scope_probe_*.py",)

# 部分校验把还原用的备份写在被测文件旁边（源码路径 + .bak），还原之后
# 并不删除。这份副本是未跟踪文件，git 恢复删不掉它，于是它同时造成两件
# 事：工作区一直报脏，而"执行前必须干净"会让**此后每一个**校验都被拒。
# 拒绝的退出码是 2，症状却是那一批校验全部未通过——排查方向会被引到
# 那批校验本身，真正的出处是留下副本的那一个。
BACKUP_SUFFIX = ".bak"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)


def dirty_guarded() -> list[str]:
    p = _git("status", "--porcelain", "--", *GUARDED)
    return [ln[3:].strip() for ln in p.stdout.splitlines() if ln.strip()]


def _untracked(rel: str) -> bool:
    return _git("ls-files", "--error-unmatch", rel).returncode != 0


def sweep_leftovers() -> list[str]:
    """删掉校验新建的临时样本文件。

    只删命中命名模式、且 git 判定为未跟踪的文件：已入库的文件不动，
    避免因误判把正常源码删掉。
    """
    removed = []
    for pattern in LEFTOVER_GLOBS:
        for path in ROOT.rglob(pattern):
            rel = path.relative_to(ROOT).as_posix()
            if ".git/" in rel:
                continue
            if not _untracked(rel):
                continue  # 已入库，不是本次新建的样本
            try:
                path.unlink()
                removed.append(rel)
            except OSError as e:
                print(f"!! 残留清理失败 {rel}：{e}")
    removed += sweep_backups()
    return removed


def sweep_backups() -> list[str]:
    """删掉留在源码目录旁的备份副本。

    只删受守护目录内、git 判定为未跟踪的 .bak。已入库的不动：那份是
    仓库里有意保留的内容，删掉等于改仓库。

    副本本身不是源码，删它不改任何源码内容。反过来说，留着它更危险：
    自愈以备份为还原依据，而备份的年代无法从文件名判断：用它覆盖源码
    等于把一份不知来源的版本写进工作区，且这一步无声。
    """
    removed = []
    for base in GUARDED:
        root = ROOT / base
        if not root.exists():
            continue
        for path in root.rglob("*" + BACKUP_SUFFIX):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if ".git/" in rel or not _untracked(rel):
                continue
            try:
                path.unlink()
                removed.append(rel)
            except OSError as e:
                print(f"!! 备份副本清理失败 {rel}：{e}")
    return removed


def restore() -> tuple[bool, list[str]]:
    """把被守护目录恢复到 HEAD，并清掉新建的样本残留。

    返回（是否做了还原，清掉的残留列表）。
    """
    swept = sweep_leftovers()
    if not dirty_guarded():
        return bool(swept), swept
    _git("checkout", "--", *GUARDED)
    leftover = dirty_guarded()
    if leftover:
        print("!! 还原后仍有残留，需人工确认：")
        for f in leftover:
            print(f"   {f}")
    return True, swept


def _looks_like_timeout(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    # 目标脚本之后的参数原样透传（例如只跑某个校验点），用于把单次耗时
    # 压到命令时限以内——整脚本连跑六次 pytest 会被中断，而中断时源文件
    # 可能停在"已注入"的状态。
    extra = sys.argv[2:]
    timeout = 600.0
    if extra and _looks_like_timeout(extra[-1]):
        timeout = float(extra.pop())

    before = dirty_guarded()
    if before:
        print(f"!! {GUARDED}/ 在执行前就不干净，拒绝执行（避免误抹改动）：")
        for f in before:
            print(f"   {f}")
        return 2

    # 沙盒的 /tmp 在命令之间会被回收，装在那里的 pytest 下一次命令就没了。
    # 落在工作区的 .pylibs 才能跨命令复用（已加入 .gitignore）。
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(ROOT / ".pylibs") + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        proc = subprocess.run([sys.executable, target, *extra], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=timeout,
                              env=env)
        out = (proc.stdout or "") + (proc.stderr or "")
        print(out.rstrip())
        code = proc.returncode
        note = ""
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        print(out.rstrip())
        print(f"\n!! 执行超时（>{timeout:.0f}s），被中断")
        code = 124
        note = "超时中断"
    finally:
        # 无论上面是怎么结束的——正常退出、异常、超时、被信号杀死——
        # 都必须走到这里。这是本脚本存在的唯一理由。
        did, swept = restore()
        if did:
            print(f"\n[已还原] {GUARDED}/ 曾被改动，现已恢复到 HEAD"
                  + (f"（{note}）" if note else ""))
        if swept:
            print(f"[已清理] 校验新建的临时样本残留：{', '.join(swept)}")

    if dirty_guarded():
        print("\n结论：源码未完全还原，需人工处理")
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
