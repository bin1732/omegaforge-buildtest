"""回退校验的判定助手：把"红了"和"抓到"分开。

只看出子进程退出码不够。两类情况都会让 rc 非零，却都不是抓到：

 · 收集阶段就失败（导入错误、语法错误）——用例一条没跑，自然一条没失败
 · 失败的是别的用例——注入点与被测行为无关

两者都满足"撤掉改动后出现失败"，于是校验点恒真。判定必须落到具体是哪几条
用例失败——只要有一条对不上号，就说明抓到的不是这一处修复。

用法::

    from probes._rev_verdict import pytest_run, verdict

    rc, failed, tail = pytest_run("tests/test_x.py")
    caught, why = verdict(failed, ["tests/test_x.py::test_y"])
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_LOCK = ROOT / ".revtmp" / "rev.lock"
_LOCK_FH = None


def _lock_pid() -> int:
    """读出锁文件里记录的持有者 PID，读不到返回 0。

    必须走已持有的句柄读，不能另开一个：Windows 的锁是强制的，锁住期间
    从别的句柄读这段字节会抛 PermissionError。而这里恰恰是"抢不到锁"之后
    被调用的——另开句柄读会把"已有脚本在跑"这条提示本身打成 PermissionError，
    症状变成锁坏了，方向完全不同。
    """
    fh = _LOCK_FH
    try:
        if fh is not None:
            fh.seek(0)
            text = fh.read()
        else:
            text = _LOCK.read_text(encoding="utf-8")
        return int((text.split() or ["0"])[0])
    except (OSError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _flock_try(fd) -> None:
    """尝试排他占用锁文件；已被占用时抛 OSError。

    两个平台的系统调用不同（Unix 的 flock 锁整个打开描述，Windows 的
    msvcrt 锁一段字节范围），但"占用不到就抛 OSError"这一点一致。
    收敛到这里，锁的接管逻辑就与平台无关，从而在任何平台上都能被验证。
    """
    if os.name == "nt":                 # pragma: no cover - Windows
        import msvcrt
        # msvcrt.locking 要整数句柄；传文件对象会抛 TypeError，而这类
        # 报错会把人引向"锁坏了"，实际只是参数类型不对。
        # 锁的是一段字节范围：空文件上锁会抛 PermissionError，而它会被
        # 读成"锁被别的进程占用"，排查方向完全不同。故先保证至少一字节。
        fd.seek(0, os.SEEK_END)
        if fd.tell() == 0:
            fd.write("0")
            fd.flush()
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _claim_lock() -> None:
    """同一仓库内只允许一个回退脚本在运行。

    两个脚本同时注入同一份源码时，后一次备份会把前一次的备份覆盖成已注入
    的版本，还原之后源码停在注入态，而此后所有校验点都在这份脏代码上跑。
    锁定把这类互相覆盖挡在运行之前，而不是等结论被污染之后再排查。

    持有者不在时锁必须能被接管
    --------------------------
    一次回退校验要跑几十轮真浏览器渲染或完整回归，被中断是常态。持有者被
    中断后若锁不可回收，**此后每一个**回退脚本都会以"已有脚本在运行"拒绝
    启动，且没有自动恢复路径——症状与"真有脚本在跑"完全一样。因此锁文件里
    记持有者 PID：抢占失败时先看这个 PID 是否还活着，死了就判定为残留并
    接管，活着的才拒绝——后者才是真正需要等待的并发。
    """
    global _LOCK_FH
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FH = open(_LOCK, "a+")
    try:
        _flock_try(_LOCK_FH)
    except OSError:
        pid = _lock_pid()
        # 记录的就是自己：本模块在导入中期与末尾各占一次锁，第二次会用新的
        # 文件描述去抢同一把锁，而 flock 按打开的文件描述计，同进程内互相
        # 冲突。此时拒绝退出会让**每一个**回退脚本都以"已有脚本在运行"自杀，
        # 提示指向并发，而实际没有任何别的进程——排查方向完全反了。本进程已
        # 持锁即无需再抢，直接继续。
        if pid == os.getpid():
            return
        if _pid_alive(pid):
            print(f"[锁] {_LOCK} 被进程 {pid} 持有：已有回退脚本在运行。并发"
                  f"注入会互相覆盖备份文件，请等它结束后再跑。")
            raise SystemExit(2)
        # 持有者已不在：判定为上一次被中断留下的残留，接管并重写 PID。
        print(f"[锁] 记录的持有者（PID {pid}）已不在，锁判定为残留，本次接管。"
              f"若此时确有别的脚本在跑，请立即中止——并发注入会互相覆盖备份。")
        try:
            _LOCK_FH.seek(0)
            _LOCK_FH.truncate()
        except OSError:
            pass
        try:
            _flock_try(_LOCK_FH)
        except OSError:
            # 记录的 PID 已死但锁仍被占用：多半是子进程继承了锁的文件描述符。
            print(f"[锁] {_LOCK} 仍被占用：有进程继承了锁描述符，"
                  f"请清理后再跑。")
            raise SystemExit(2)
    _LOCK_FH.seek(0)
    _LOCK_FH.truncate()
    _LOCK_FH.write(f"{os.getpid()}\n")
    _LOCK_FH.flush()


def _running_as_rev_script() -> bool:
    name = Path(getattr(sys.modules.get("__main__"), "__file__", "") or "").name
    return name.startswith("rev") or name.startswith("revert")


# --------------------------------------------------------------------------
# 中断自愈
# --------------------------------------------------------------------------
# 回退脚本的流程是"备份 → 注入 → 跑用例 → 还原"。进程在注入之后、还原之前
# 被中断时（超时、被杀、连接断开），还原那一步不会执行，源码停在注入态。
# 此后每个校验点都在这份脏代码上跑，症状是"某个点名对不上"——排查方向会被
# 带到产品代码上，实际坏的是上一次没走完的流程。
#
# 两道记录用来在下次启动时把源码对齐回去：
#   · 登记文件：注入前写入（源文件, 备份），还原成功后注销，精确。
#   · 备份目录：串行锁保证同一时刻只有一个脚本在跑，于是目录里残留的备份
#     必然是上一次没走完留下的，按文件名回查源码位置即可兜底。
_REGISTRY = ROOT / ".revtmp" / "registry.tsv"
_BACKUP_DIR = Path(os.environ.get("REV_BACKUP_DIR",
                                  "/data/workspace/.rev_backups"))


def _reg_read() -> list[tuple[str, str]]:
    if not _REGISTRY.exists():
        return []
    out = []
    for ln in _REGISTRY.read_text(encoding="utf-8").splitlines():
        if "\t" in ln:
            a, b = ln.split("\t", 1)
            out.append((a.strip(), b.strip()))
    return out


def _reg_write(rows: list[tuple[str, str]]) -> None:
    _REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY.write_text("".join(f"{a}\t{b}\n" for a, b in rows),
                         encoding="utf-8")


def reg_add(src: Path, bak: Path) -> None:
    """登记一次进行中的注入。必须在写备份之后、改源码之前调用。"""
    rows = [(a, b) for a, b in _reg_read() if a != str(src)]
    rows.append((str(src), str(bak)))
    _reg_write(rows)


def reg_remove(src: Path) -> None:
    rows = [(a, b) for a, b in _reg_read() if a != str(src)]
    _reg_write(rows)


def _locate_src(bak: Path) -> Path | None:
    """按备份文件名回查源码位置；同名文件多于一个时不猜，返回 None。"""
    stem = bak.name[:-len(".bak")] if bak.name.endswith(".bak") else bak.name
    for sub in ("omegaforge", "scripts", "tests", "probes", "frontend"):
        base = ROOT / sub
        if not base.exists():
            continue
        hits = list(base.rglob(stem))
        if len(hits) == 1:
            return hits[0]
    return None


def self_heal() -> list[str]:
    """把未完成的那次注入还原回去，返回被还原的文件名（空列表即无需自愈）。

    登记非空说明上一次没走完；登记为空不代表干净——备份目录里残留的备份同
    样意味着中断。两条都查，还原成功即注销并删除备份。备份缺失时退回版本
    库：受版本控制的文件在 HEAD 上的内容就是干净版本。
    """
    pending: dict[str, str] = {}
    for src, bak in _reg_read():
        pending[src] = bak
    if _BACKUP_DIR.is_dir():
        for bak in sorted(_BACKUP_DIR.glob("*.bak")):
            try:
                if bak.stat().st_size == 0:
                    continue
            except OSError:
                continue
            src = _locate_src(bak)
            if src is not None:
                pending.setdefault(str(src), str(bak))
    # 备份写在源文件旁边（多数脚本的写法）时，备份目录里什么都没有，只查
    # 备份目录会让注入残留永远发现不了：源码停在注入态，此后每个校验点都在
    # 这份脏代码上跑，症状表现为点名对不上，排查方向被带到产品代码上。
    # 同目录备份的源码位置无需回查——去掉 .bak 后缀即是。
    for sub in ("omegaforge", "scripts", "tests", "probes", "frontend"):
        base = ROOT / sub
        if not base.exists():
            continue
        for bak in sorted(base.rglob("*.bak")):
            src = bak.with_name(bak.name[: -len(".bak")])
            if src.is_file():
                pending.setdefault(str(src), str(bak))
    if not pending:
        return []

    healed = []
    for src_s, bak_s in sorted(pending.items()):
        src, bak = Path(src_s), Path(bak_s)
        if bak and Path(bak).exists():
            try:
                restore_src(src, Path(bak))
                # 还原后必须与 HEAD 复核：备份本身可能就是注入态（例如某次
                # 提交把 .bak 收进了版本库，此后每次自愈都拿它当干净版本）。
                # 只信备份不复核的话，自愈会一路报"已还原"，而源码始终停在
                # 注入态——后续校验点全在这份脏代码上跑，症状表现为点名对不
                # 上；更糟的是有人照提示"先提交"就把注入态当源码提交了。
                chk = subprocess.run(
                    ["git", "diff", "--quiet", "--", str(src)], cwd=str(ROOT),
                    capture_output=True, text=True)
                if chk.returncode == 0:
                    healed.append(f"{src.name}（备份）")
                    reg_remove(src)
                    continue
                healed.append(f"{src.name}（备份无效，改走版本库）")
            except OSError:
                pass
        p = subprocess.run(["git", "checkout", "--", str(src)], cwd=str(ROOT),
                           capture_output=True, text=True)
        if p.returncode == 0:
            healed.append(f"{src.name}（版本库）")
        reg_remove(src)
        try:
            Path(bak).unlink()
        except OSError:
            pass
    purge_pyc()
    return healed


if _running_as_rev_script() and os.environ.get("REV_NO_LOCK") != "1":
    _claim_lock()


def _pytest_env() -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from probes._pytest_env import env as _env
    return _env()


def purge_pyc() -> int:
    """清掉仓库内的字节码缓存，返回删除的缓存文件数。

    为什么必须在每次跑用例前清
    --------------------------
    回退校验的流程是"注入源码 → 跑用例 → 还原源码"。备份用 copy2、还原用
    move，两者**都保留原文件的 mtime**；而 Python 判断是否复用 `.pyc`
    看的是**源码 mtime + 文件大小**。于是当注入与还原后的源码长度恰好
    相同时（例如把 `return 1` 改成 `return 0`），还原之后 `.pyc` 里记录
    的 mtime 与大小仍与源码吻合——**后续跑用例执行的仍是注入期间编译出的
    字节码**。

    后果比"注入没生效"更坏：源码看起来已经还原，基线也报全绿，但绿的
    是注入版的行为，"还原后仍全绿"这条自检同样被骗过。

    每次跑用例前清一次，Pyhton 就只能按当前源码重新编译，这条隐患在
    所有用本助手的脚本里一并消掉。
    """
    n = 0
    for sub in ("omegaforge", "scripts", "tests", "probes"):
        base = ROOT / sub
        if not base.exists():
            continue
        for d in base.rglob("__pycache__"):
            for f in d.glob("*.pyc"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def restore_src(path: Path, bak: Path) -> None:
    """把备份还原回源码，并确保不会接着用注入期间编译出的字节码。

    除了 move 本身，还要做两件事：刷新 mtime（让同尺寸的 `.pyc` 立刻
    失效）与删掉该模块对应的缓存文件。只做 move 的话，同尺寸还原会被
    Python 判成"源码没变"，继续用注入期间那份字节码——见 purge_pyc 说明。
    """
    import os

    import shutil as _shutil
    _shutil.move(str(bak), str(path))
    # 还原成功即注销：登记只用来表示"有注入尚未走完"。
    reg_remove(path)
    try:
        os.utime(path, None)
    except OSError:
        pass
    cache = path.parent / "__pycache__"
    if cache.exists():
        for f in cache.glob(f"{path.stem}.*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass


def pytest_run(test_path: str, timeout: int = 900) -> tuple[int, list, str]:
    """跑一次用例，返回（退出码，失败的用例标识列表，末行摘要）。

    加 -rf 才能让 pytest 列出失败用例的标识；没有它只能看到一个数字，
    无法区分"预期的用例失败了"和"别的用例失败了"。
    """
    purge_pyc()
    p = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q", "--no-header",
         "-rf", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
        timeout=timeout, env=_pytest_env())
    lines = (p.stdout or "").splitlines()
    failed = [ln.strip()[len("FAILED "):].split(" - ")[0].strip()
              for ln in lines if ln.strip().startswith("FAILED ")]
    # 子测试（unittest subTest）的失败不走 FAILED 行，而是
    # "SUBFAILED[param] path::test"。不计入的话，一个全部以子测试形式
    # 失败的用例会被当成"没失败"——校验点从抓到翻成未抓到，而翻面方向
    # 表现为校验失败，容易被当成"这处修复没生效"去改被测代码。
    # 子测试归入其父用例：按基名归并时本就同一条，不重复计。
    for ln in lines:
        t = ln.strip()
        if not t.startswith("SUBFAILED"):
            continue
        parts = t.split(None, 1)
        if len(parts) == 2 and "::" in parts[1]:
            ident = parts[1].split(" - ")[0].strip()
            if ident not in failed:
                failed.append(ident)
    tail = lines[-1].strip() if lines else ""
    return p.returncode, failed, tail


def verdict(rc: int, failed: list, expect: list,
            strict: bool = True) -> tuple[bool, str]:
    """判定是否抓到：预期的用例必须真的失败，且不能是靠报错凑出来的红。

    点名只解决了"失败的是别的用例"，解决不了"整片都红了"。给被
    测模块加一个模块级未定义名，用例文件里的 3 条用例**全部**失败——
    预期的那一条自然也在里面，于是任何点名都会被满足。这种红是环境故障，
    不是守卫在起作用。

    因此默认严格：出现预期之外的失败项同样判未抓到，并把多出来的列出来。
    宽松模式（strict=False）只用于本来就预期会连带多条的历史校验点。
    """
    if rc == 0:
        return False, "用例全绿（修复被撤掉后仍无失败项）"
    if not failed:
        # rc 非零却没有任何 FAILED 行，基本都是收集阶段就炸了——
        # 用例一条没跑，谈不上抓到。
        return False, f"rc={rc} 但没有失败用例（多为收集阶段报错，不可作为证据）"
    # 用例标识可能带类名（Class::test_x）与参数化后缀（test_x[a]），
    # 统一取最后一段再比，否则写全路径永远对不上。
    def leaf(x: str) -> str:
        # 不能直接 split("::")[-1]：参数化标识里可能含 "::"——内网地址
        # 用例的参数就是 IPv6 字面量（http://[::1]/），那样切会剩下 "1]/]",
        # 与任何期望都对不上，于是正常的失败被判成"预期之外还失败"。
        # 正确切法：先去掉第一段（文件路径，以 .py 结尾），再去掉可选的
        # 类名段（不含 "[" 的那一段），剩下的整体才是用例标识。
        parts = x.split("::")
        # 首段可能是文件路径，也可能是**空串**：跨盘符时相对路径算不出来，
        # 输进来的标识会以 "::" 开头。漏掉空串这一段，同一条用例在 Linux
        # 上归一成 test_x、在 Windows 上归一成 T::test_x，于是点名对不上，
        # 校验点从"抓到"翻成"未抓到"——而翻面方向会被当成修复没生效。
        while parts and (parts[0] == "" or parts[0].endswith(".py")):
            parts = parts[1:]
        if not parts:
            return x
        if len(parts) > 1 and "[" not in parts[0]:
            parts = parts[1:]
        return "::".join(parts)

    def _base(x: str) -> str:
        # 参数化用例的标识带 [param] 后缀。同一条用例的不同参数实例是同
        # 一次注入的自然结果，按基名归并才不会把它们当成"多出来的失败"。
        return x.split("[")[0]

    def _matches(g: str, exp_leaves: list) -> bool:
        # 期望里写了实例后缀时按实例精确比。同一条用例的不同参数实例可能
        # 对应**完全不同**的被测分支（例如三个实例分别走三种边界标记），
        # 按基名归并会把"撤掉其中一种"判成抓到，而哪一种被撤掉无从分辨。
        for e in exp_leaves:
            if "[" in e:
                if g == e:
                    return True
            elif _base(g) == _base(e):
                return True
        return False

    got = [leaf(f) for f in failed]
    exp_leaves = [leaf(e) for e in expect]
    missing = [e for e, le in zip(expect, exp_leaves)
               if not any(_matches(g, [le]) for g in got)]
    if missing:
        return False, (f"有失败但不是预期的用例：缺 {missing}；"
                       f"实际失败 {failed[:5]}")
    if strict:
        unexpected = [f for f, g in zip(failed, got)
                      if not _matches(g, exp_leaves)]
        if unexpected:
            return False, (f"整片都红了：预期之外还失败 {unexpected[:5]}"
                           f"（多是被测模块整体不可用，不是守卫在起作用）")
    return True, f"预期用例失败：{failed[:5]}"


# --------------------------------------------------------------------------
# 导入时即生效：先占锁，再把未完成的那次注入对齐回去。
# 这两步必须放在模块末尾——它们依赖本模块后半部分定义的 restore_src 与
# purge_pyc，放在中部会在导入阶段拿到未定义的名字。
# --------------------------------------------------------------------------
if _running_as_rev_script() and os.environ.get("REV_NO_LOCK") != "1":
    _claim_lock()
    _healed = self_heal()
    if _healed:
        print(f"[自愈] 上一次注入未走完，已还原：{', '.join(_healed)}")
        print("       （残留会让后续校验点在注入态源码上跑，"
              "症状表现为点名对不上，排查方向会被带到产品代码上）")
