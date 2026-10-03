"""中断自愈的守卫：注入未走完时，下次启动必须把源码对齐回去。

回退脚本的流程是「备份 → 注入 → 跑用例 → 还原」。进程在注入之后、还原
之前被中断时（超时、被杀、连接断开），还原那一步不会执行，源码停在注入
态，此后每个校验点都在这份脏代码上跑。症状表现为「点名对不上」，与产品
缺陷在输出上无法区分——排查方向会被带到产品代码上。

三件事缺一不可：

 · 登记非空时必须还原：精确路径，能定位到唯一源文件
 · 备份目录残留时必须还原：按文件名回查源码位置，用于登记缺失的情形
 · 两者都为空时不得改动任何文件：误伤会让合法改动被悄悄回退，而回退
   本身无声，事后查不出来

第三条对应一类特别的失效：自愈写成「无条件还原」同样能通过前两条，代价
是把正在编辑的文件一起还原掉。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import _rev_verdict as v  # noqa: E402

CLEAN = "VALUE = 1\n"
DIRTY = "VALUE = 0\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """一个最小仓库：omegaforge 下有唯一一个 target.py。"""
    (tmp_path / "omegaforge").mkdir()
    src = tmp_path / "omegaforge" / "target.py"
    src.write_text(CLEAN, encoding="utf-8")
    bakdir = tmp_path / "baks"
    bakdir.mkdir()
    monkeypatch.setattr(v, "ROOT", tmp_path)
    monkeypatch.setattr(v, "_REGISTRY", tmp_path / ".revtmp" / "registry.tsv")
    monkeypatch.setattr(v, "_BACKUP_DIR", bakdir)
    return tmp_path, src, bakdir


def test_registry_pending_is_restored(repo):
    tmp_path, src, bakdir = repo
    bak = bakdir / "target.py.bak"
    bak.write_text(CLEAN, encoding="utf-8")
    src.write_text(DIRTY, encoding="utf-8")
    v.reg_add(src, bak)

    healed = v.self_heal()

    assert src.read_text(encoding="utf-8") == CLEAN
    assert healed, "登记非空却报告无需自愈"
    assert not bak.exists(), "还原后备份必须一并清掉，否则下次启动重复还原"


def test_orphan_backup_is_restored_by_filename(repo):
    """登记缺失时，备份目录里的残留仍要能回查到源码。"""
    tmp_path, src, bakdir = repo
    (bakdir / "target.py.bak").write_text(CLEAN, encoding="utf-8")
    src.write_text(DIRTY, encoding="utf-8")

    healed = v.self_heal()

    assert src.read_text(encoding="utf-8") == CLEAN
    assert healed


def test_sibling_backup_is_restored(repo):
    """备份写在源文件旁边时必须同样能自愈。

    多数回退脚本把备份写成 `源码.py.bak` 放在同目录，备份目录里什么都没
    有。只扫备份目录的话这类残留永远发现不了：源码停在注入态，此后每个
    校验点都在这份脏代码上跑，症状表现为点名对不上，排查方向会被带到产
    品代码上。
    """
    tmp_path, src, bakdir = repo
    (src.parent / "target.py.bak").write_text(CLEAN, encoding="utf-8")
    src.write_text(DIRTY, encoding="utf-8")

    healed = v.self_heal()

    assert src.read_text(encoding="utf-8") == CLEAN
    assert healed
    assert not (src.parent / "target.py.bak").exists()


def test_nothing_pending_leaves_files_untouched(repo):
    """没有中断迹象时不得改动任何文件——误伤会把合法改动悄悄回退。"""
    tmp_path, src, bakdir = repo
    src.write_text(DIRTY, encoding="utf-8")

    healed = v.self_heal()

    assert healed == []
    assert src.read_text(encoding="utf-8") == DIRTY, (
        "干净状态下自愈改了源码：合法改动会被无声回退")


def test_second_call_is_idempotent(repo):
    tmp_path, src, bakdir = repo
    bak = bakdir / "target.py.bak"
    bak.write_text(CLEAN, encoding="utf-8")
    src.write_text(DIRTY, encoding="utf-8")
    v.reg_add(src, bak)

    v.self_heal()
    assert v.self_heal() == []


def test_locate_src_needs_unique_match(repo):
    """同名文件多于一个时不得猜测——还原错文件比不还原更坏。"""
    tmp_path, src, bakdir = repo
    dup = tmp_path / "omegaforge" / "sub"
    dup.mkdir()
    (dup / "target.py").write_text(DIRTY, encoding="utf-8")
    assert v._locate_src(bakdir / "target.py.bak") is None


# --------------------------------------------------------------------------
# 互斥锁：持有者不在时必须能被接管
# --------------------------------------------------------------------------
# 回退校验常常跑几十轮真浏览器渲染或完整回归，被中断是常态。持有者被杀后
# 若锁不可回收，此后每一个回退脚本都以"已有脚本在运行"拒绝启动，且没有自动
# 恢复路径——症状与"真有脚本在跑"完全一样。因此锁文件记持有者 PID：抢占
# 失败时先看这个 PID 是否还活着。
def _pid_in_lock(v) -> int:
    """经持有中的句柄读锁文件内容。

    另开句柄读在 Windows 上行不通：锁是强制的，锁住期间从别的句柄读这段
    字节会抛 PermissionError。断言因此必须走同一个句柄。
    """
    fh = v._LOCK_FH
    fh.seek(0)
    return int(fh.read().strip())


def test_lock_records_current_pid(tmp_path, monkeypatch):
    lock = tmp_path / "rev.lock"
    monkeypatch.setattr(v, "_LOCK", lock)
    v._claim_lock()
    assert _pid_in_lock(v) == os.getpid()
    # 抢不到锁之后要读持有者 PID：此时锁在别人手里，读必须走已持有的句柄，
    # 否则 Windows 上会以 PermissionError 失败，而不是给出"已有脚本在跑"。
    assert v._lock_pid() == os.getpid()


def test_dead_holder_lock_is_taken_over(tmp_path, monkeypatch):
    """记录的持有者已不在：必须接管，而不是一律拒绝。

    若改成"抢占失败即退出"，上一次被中断留下的锁会让此后所有脚本拒绝运行。

    占用失败通过挂载点模拟，不直接碰 fcntl：fcntl 在 Windows 上不存在，
    那样写会让整批用例以 ModuleNotFoundError 失败——看起来像锁坏了，
    实际只是这一平台没有这个模块。
    """
    lock = tmp_path / "rev.lock"
    lock.write_text("99999\n", encoding="utf-8")   # 一个不存在的 PID
    monkeypatch.setattr(v, "_LOCK", lock)
    monkeypatch.setattr(v, "_pid_alive", lambda pid: False)

    calls = {"n": 0}

    def fl_once(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("模拟被占用")

    monkeypatch.setattr(v, "_flock_try", fl_once)
    v._claim_lock()                                  # 不得抛 SystemExit
    assert calls["n"] == 2, "持有者不在时应重试抢占一次"
    assert _pid_in_lock(v) == os.getpid()


def test_live_holder_is_refused(tmp_path, monkeypatch):
    """持有者还活着时必须拒绝——那才是真正需要等待的并发。"""
    lock = tmp_path / "rev.lock"
    lock.write_text("1\n", encoding="utf-8")        # PID 1 恒存活
    monkeypatch.setattr(v, "_LOCK", lock)

    def fl_always_busy(fd):
        raise OSError("busy")

    monkeypatch.setattr(v, "_flock_try", fl_always_busy)
    with pytest.raises(SystemExit) as e:
        v._claim_lock()
    assert e.value.code == 2


def test_own_pid_lock_is_not_refused(tmp_path, monkeypatch):
    """记录的是自己时不得拒绝——本进程已持锁，再抢一次只会撞到自己。

    抢锁失败后若只看"这个 PID 还活着"，本进程会把自己判成"已有脚本在运
    行"并退出。症状是每一个回退脚本都在启动时自杀，提示指向并发，而实际
    没有任何别的进程在跑——排查方向完全反了。

    判据不能用模块内的句柄：模块被以两个名字各导入一次时句柄是两份，进
    程内幂等失效；也不能用"进程内只抢一次"的全局标记，那样会把同进程内
    的第二次调用整个跳过，令本条守卫恒真。

    只看"没抛 SystemExit"不够——一进来就 return 的空实现同样不抛，而它
    连锁文件都没打开。故再断言句柄已保留：这条路径下 `_lock_pid()` 后续
    仍要靠它读，句柄为 None 会退化成另开句柄读，Windows 上正是 PermissionError
    的来源，症状会变成"锁坏了"。
    """
    lock = tmp_path / "rev.lock"
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(v, "_LOCK", lock)
    monkeypatch.setattr(v, "_pid_alive", lambda pid: True)   # 自己当然活着

    def fl_always_busy(fd):
        raise OSError("busy")

    monkeypatch.setattr(v, "_flock_try", fl_always_busy)
    v._claim_lock()          # 不得抛 SystemExit

    assert v._LOCK_FH is not None, (
        "判定锁为自己所持后仍须保留句柄：空实现一进来就 return，同样不抛异常"
    )
    # 这条路径是提前 return，不得改写锁文件内容（那会把持有者信息清掉）。
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid()), (
        "锁文件内容须保持为本进程 PID：提前 return 的路径不应改写它"
    )
