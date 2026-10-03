"""原子写与跨进程文件锁——个人数据存储的底层基建。

为什么需要这个模块
--------------------
本仓有 11 处「原子写」，写法都是：

    tmp = self.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(...)
    os.replace(tmp, self.path)

`tmp + replace` 的意义是让读者永远看不到半成品。但**临时文件名是固定的**，
于是并发写会互相踩：

    线程 A: open(tmp) -> 截断 -> 开始写 600KB
    线程 B: open(tmp) -> 截断 -> 从偏移 0 开始写 600KB   ← 同一个文件！
    A: os.replace(tmp, path)
    B: os.replace(tmp, path)                              ← 又一次

两个写入者持有**同一个临时文件**，各自的文件偏移互不知情。后果有三档，
从轻到重：

    1. 后写覆盖先写 —— 丢更新（例如 12 条待办只活下来 1~2 条）
    2. 内容交错 —— 产出拼接的坏 JSON：
       json.decoder.JSONDecodeError: Extra data: line 77 column 4
       该文件此后**永久不可解析**，用户数据直接消失
    3. 序列化途中字典被别的线程改动：
       RuntimeError: dictionary changed size during iteration
       —— 从 _save() 冒泡到请求线程，表现为 500

也就是说：**本想靠临时文件获得原子性，恰恰是临时文件名本身毁掉了原子性。**
服务端用的是 ThreadingHTTPServer，两个并发请求就会触发；CLI / MCP / 桌面端
同时打开同一份数据时是跨进程触发，概率更高。

修法三件事
----------
1. `atomic_write()`：临时文件名带 pid / 线程 id / 随机后缀，**每个写入者独占**；
   写完 fsync 再 replace，并对目录 fsync（尽力而为，失败不影响正确性）。
2. `file_lock()`：跨进程互斥，用于「读盘 → 改内存 → 全量写回」这类
   读-改-写临界区。它挡不住 tmp 踩踏（那是第 1 条的事），但能挡住丢更新。
3. `atomic_write` 只保证「文件不会坏」，不保证「更新不丢」。丢更新要靠
   file_lock 把读-改-写包起来。二者职责不同，不要互相替代。

平台
----
零依赖，纯 stdlib。Unix 用 fcntl.flock；Windows 用 msvcrt.locking；两者都
不可用时回退到 O_EXCL 占位文件 + 陈旧锁清理。桌面端（Tauri）三平台都要能跑，
所以不能只写 fcntl。
"""

from __future__ import annotations

import contextlib
import os
import random
import threading
import time
import uuid

__all__ = ["atomic_write", "atomic_write_json", "file_lock", "LOCK_TIMEOUT",
           "_replace"]

# 跨进程锁的等待上限。超过就抛出异常而不是无限等——卡住的锁必须能被发现，
# 静默等待会让界面表现为"点了没反应"，那是最难排查的一类故障。
LOCK_TIMEOUT = 10.0

# 陈旧锁的判定阈值：占位文件存在超过这个秒数就认为持有者已死。
# 取 LOCK_TIMEOUT 的 4 倍——正常临界区是毫秒级，4 倍足够宽松。
_STALE_SECONDS = LOCK_TIMEOUT * 4

# os.replace 遇到瞬态共享冲突时的重试上限（秒）。
# 取 1 秒：并发替换的持有窗口是毫秒级，几个来回足够；再长就说明目标被
# 长期占用（例如另一进程一直开着它），那时等下去只会把"保存"变成卡顿。
REPLACE_RETRY_SECONDS = 1.0


def _unique_tmp(path: str) -> str:
    """每个写入者独占的临时文件名。

    固定名是踩踏的根源，所以这里把 pid、线程 id 和随机串都拼进去。
    随机串是必要的：同一进程先后两次写入若只用 pid+tid，理论上仍可能
    与上一次未清理的残留同名。
    """
    base = os.path.basename(path)
    return os.path.join(
        os.path.dirname(path) or ".",
        f".{base}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp",
    )


def _fsync_dir(path: str) -> None:
    """对目录 fsync，让 replace 真正落盘（崩溃时不会丢）。

    某些平台/文件系统不支持对目录 fd 做 fsync，失败一律忽略——它只影响
    掉电场景的持久性，不影响正常运行时的正确性。
    """
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _replace(tmp: str, path: str, *, retry_seconds: float | None = None,
             _impl=None) -> None:
    """把 tmp 换成 path。

    Windows 上 `os.replace` 会因目标文件被别的句柄持有而抛
    PermissionError（WinError 5，共享冲突）：目标此刻正被另一个写入者
    替换、或被索引/查毒进程短暂打开，都会触发。它不是权限问题，而是**瞬态**
    ——同一条语句稍后重放即成功。

    不做重试的后果是真实的：桌面端首要平台就是 Windows，保存待办/知识/
    会话时抛出 PermissionError，使用者看到的是"保存失败"，而数据其实没
    问题。这类故障只在真机并发下现身，本地串行跑永远绿。

    等待时长按平台给：Windows 给 REPLACE_RETRY_SECONDS，其余平台给 0。
    重试循环本身在所有平台上都跑，这样"遇到瞬态冲突会重试、重试到底会抛"
    这两件事在任何平台上都能被验证，不需要一台 Windows 才能证明。

    达到上限后原样抛出，不吞异常——换成"尽力而为"会让写入静默丢失，
    那比报错更难查。
    """
    impl = _impl or os.replace
    if retry_seconds is None:
        retry_seconds = REPLACE_RETRY_SECONDS if os.name == "nt" else 0.0
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            impl(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005 + random.random() * 0.02)


def atomic_write(path: str, text: str, *, encoding: str = "utf-8") -> None:
    """把 text 原子地写成 path：要么读到旧内容，要么读到新内容，没有中间态。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        # 写失败就别留垃圾：残留的 .tmp 会被目录遍历当成数据文件。
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path)


def atomic_write_json(path: str, data, *, indent: int | None = 1,
                      ensure_ascii: bool = False) -> None:
    """atomic_write 的 JSON 便捷入口。

    注意 `sort_keys` 不开启：既有产物的键顺序是有意的（可读 diff）。
    """
    import json
    atomic_write(path,
                 json.dumps(data, ensure_ascii=ensure_ascii, indent=indent))


# -- 跨进程锁 ------------------------------------------------------------

try:                                    # pragma: no cover - 平台分支
    import fcntl
except ImportError:                     # pragma: no cover - Windows
    fcntl = None
try:                                    # pragma: no cover - 平台分支
    import msvcrt
except ImportError:                     # pragma: no cover - Unix
    msvcrt = None


class LockTimeout(RuntimeError):
    """等锁超时。刻意不用 TimeoutError——后者容易被当成网络问题误判。"""


def _acquire_flock(fd: int, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LockTimeout("等待数据文件锁超时")
            time.sleep(0.01 + random.random() * 0.01)


def _release_flock(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire_msvcrt(fd: int, deadline: float) -> None:
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LockTimeout("等待数据文件锁超时")
            time.sleep(0.01 + random.random() * 0.01)


def _release_msvcrt(fd: int) -> None:
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _acquire_excl(holder: str, deadline: float) -> int:
    """O_EXCL 占位文件回退：fcntl / msvcrt 都不可用时使用。

    占位文件里写入 pid，并在超时的情况下按 mtime 判定陈旧后清理——否则
    一个崩溃的进程会让锁永久悬挂，而这正是"界面点了没反应"的来源。
    """
    while True:
        try:
            fd = os.open(holder, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with contextlib.suppress(OSError):
                os.write(fd, str(os.getpid()).encode())
            return fd
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(holder)
            except OSError:
                age = 0.0
            if age > _STALE_SECONDS:
                with contextlib.suppress(OSError):
                    os.unlink(holder)
                continue
            if time.monotonic() >= deadline:
                raise LockTimeout("等待数据文件锁超时")
            time.sleep(0.01 + random.random() * 0.01)


# 本进程已持有的锁：{(线程 id, lock_path): 引用计数}。
#
# 为什么需要：flock 的锁属于"打开文件描述"，**同一进程内两个不同的 fd 也会
# 互相冲突**（man flock 明确写了这一点）。而调用链天然会嵌套——例如
# KnowledgeBase.remember() 内部调 add()，若两层都去 flock 就会自己等自己，
# 一直等到超时。这不是理论风险，是必然发生。
#
# 所以同一线程内只真正加一次锁，嵌套调用走引用计数。
#
# 键里**必须带线程 id**：若只用锁文件路径作键，「线程 A 持锁期间，线程 B
# 进入 file_lock」会被判定成 A 的嵌套重入 → B 直接放行、根本不加锁。
#
# 靠"调用方自己用 RLock 保证线程间互斥"这个约定不可靠：只要有一个调用点
# 没有配 RLock，并发写入就会互相覆盖——服务端是 ThreadingHTTPServer，两个
# 并发请求即触发，而失效时没有任何报错，用户看到"保存成功"，数据却没了。
#
# 所以线程隔离由 file_lock 自己保证，不把前提转嫁给调用方。
_HELD: dict[tuple[int, str], int] = {}
_HELD_GUARD = threading.Lock()


def _held_key(lock_path: str) -> tuple[int, str]:
    return (threading.get_ident(), lock_path)


@contextlib.contextmanager
def file_lock(path: str, timeout: float = LOCK_TIMEOUT):
    """对 `path` 对应的数据文件加跨进程排他锁。

    锁文件是 `path + ".lock"`，与数据文件分离——这样读数据的人永远读不到
    锁内容，且 `atomic_write` 的临时文件不会被误当成锁。
    """
    lock_path = str(path) + ".lock"
    key = _held_key(lock_path)
    with _HELD_GUARD:
        n = _HELD.get(key, 0)
        if n:
            _HELD[key] = n + 1
            acquired = False
        else:
            _HELD[key] = 1
            acquired = True
    if not acquired:
        try:
            yield
            return
        finally:
            with _HELD_GUARD:
                _HELD[key] = max(0, _HELD.get(key, 1) - 1)
    d = os.path.dirname(lock_path)
    try:
        if d:
            os.makedirs(d, exist_ok=True)
        deadline = time.monotonic() + (timeout or LOCK_TIMEOUT)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except BaseException:
        # 拿不到 fd 就必须撤掉上面预占的计数。
        #
        # 建目录与打开锁文件一旦失败（磁盘满、权限变更、路径被换成目录等），
        # 计数会永久停留——此后本线程再进入时会被判定成"重入"而直接放行，
        # file_lock 退化成空操作且调用方毫不知情。这个状态不随故障解除而
        # 恢复：故障修好后，该线程仍然永远不加锁。
        #
        # 服务端每请求一个线程，泄漏随线程销毁；而长期存活的线程（命令行、
        # 主循环）一旦泄漏，余下的跨进程互斥全部失效。
        with _HELD_GUARD:
            _HELD.pop(key, None)
        raise
    try:
        if fcntl is not None:
            _acquire_flock(fd, deadline)
            try:
                yield
            finally:
                _release_flock(fd)
        elif msvcrt is not None:        # pragma: no cover - Windows
            _acquire_msvcrt(fd, deadline)
            try:
                yield
            finally:
                _release_msvcrt(fd)
        else:                           # pragma: no cover - 双回退
            os.close(fd)
            hfd = _acquire_excl(lock_path + ".holder", deadline)
            try:
                yield
            finally:
                # 先关句柄再删：Windows 不允许删除仍被打开的文件，顺序反了
                # 删除必定失败（而失败被 suppress 吞掉），占位文件留下，
                # 下一次取锁要等它判为陈旧——表现为每次写数据都卡满超时，
                # 界面"点了没反应"。
                with contextlib.suppress(OSError):
                    os.close(hfd)
                with contextlib.suppress(OSError):
                    os.unlink(lock_path + ".holder")
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with _HELD_GUARD:
            _HELD[key] = max(0, _HELD.get(key, 1) - 1)
