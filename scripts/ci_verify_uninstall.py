# -*- coding: utf-8 -*-
"""卸载流程验收：装上之后，用户能不能真的卸掉。

## 验的是什么

安装、启动、逐功能验收都做了，唯独卸载没人走过一遍。
而"卸不掉"与"卸完留一堆"都是真实的用户问题，且都不会表现为
"安装失败"——装的时候一切正常，问题只发生在用户想删掉它的时候。

## 为什么要带着正在运行的应用卸载

验收步骤若一律"用完就停"，卸载就总是在停掉之后才做，
而真实用户是在应用开着的时候去卸载的。这条路径上有两类失效：

  1. 卸载器被还在运行的服务进程占住文件，卡死不返回；
     用户看到的是"正在卸载…"一直转，最后只能强杀。
  2. 卸载器删掉了文件但没停服务，进程仍在后台跑；
     用户以为卸掉了，实际还在占端口、还在写数据。

两者在"先停再卸"的流程里都看不见。

## 判定

  * 卸载器必须存在（安装产物里没有卸载入口 = 用户无法卸载）
  * 卸载器必须在超时前自行退出（卡死即失败）
  * 卸载后主程序不再存在
  * 卸载后服务进程不再存活（残留即失败）

用户数据目录是否保留属于安装器的设计选择，本脚本只如实记录，
不代用户决定——不知道的就不判，免得把设计意图记成缺陷。

用法：
  python scripts/ci_verify_uninstall.py <安装目录> [端口]
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import install_layout_checks as ilc  # noqa: E402

UNINSTALL_TIMEOUT = int(os.environ.get("OF_UNINSTALL_TIMEOUT", "180"))


def _main_exe_names(install_dir: str) -> list[str]:
    """主程序可能的名字。

    安装器会把产品名里的空格写成连字符，所以按归一化后的名字回查实际产物，
    而不是拿 productName 直接比对文件名——两者对不上时，"产物缺主程序"与
    "名字写法不同"症状完全一样，会把排查带去查打包配置。
    """
    app = ilc.product_name()
    if not app:
        return []
    found = ilc.find_file(install_dir, app, ".exe")
    return [os.path.basename(found)] if found else [app]


def _probe(port: int, tries: int = 60) -> bool:
    """探活：连得上且是本服务才算就绪。"""
    import urllib.request
    for _ in range(tries):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _runtime_residual(install_dir: str, residual: list[str]) -> list[str]:
    """卸载后不得残留运行时。

    卸载器删不掉被占用的文件时会静默跳过且不再重试。本程序只要晚一步退出，
    整个运行时（约两百多 MB）就永久留在用户磁盘上，而卸载界面显示的是卸载
    完成——用户以为卸干净了，磁盘却没释放。因此残留必须判失败，不能只打印。
    """
    out: list[str] = []
    if "_internal" in residual:
        n = 0
        try:
            for _r, _d, fs in os.walk(os.path.join(install_dir, "_internal")):
                n += len(fs)
        except OSError:
            n = -1
        out.append(f"卸载后运行时目录仍在（_internal，{n} 个文件）")
        out.append("     症状：卸载界面显示完成，磁盘上仍留着整棵运行时")
    for name in residual:
        if name.lower().endswith(".exe") and not name.lower().startswith("uninstall"):
            out.append(f"卸载后可执行文件仍在：{name}")
            out.append("     症状：卸载器删不掉被占用的文件会静默跳过，之后不再重试")
    out.extend(_stray_home_residual(install_dir, residual))
    return out


def _stray_home_residual(install_dir: str, residual: list[str]) -> list[str]:
    """安装目录里不得留下数据目录。

    打包运行时的数据目录一律落在用户目录，安装目录出现数据目录说明有进程
    把当前工作目录当成了 home。这类目录不在卸载清单里，卸载后永久留下；更
    要紧的是它意味着数据被写了两份，而用户看得见、用得上的只有用户目录那
    一份——另一份是无人知晓的孤儿。故判失败并打印内容，便于定位写入方。
    """
    out: list[str] = []
    for name in residual:
        if name != ".omegaforge":
            continue
        d = os.path.join(install_dir, name)
        items: list[str] = []
        try:
            for _r, _ds, fs in os.walk(d):
                for f in fs:
                    items.append(os.path.relpath(os.path.join(_r, f), d))
        except OSError:
            items = ["<读取失败>"]
        out.append(f"卸载后安装目录里残留数据目录（{name}，{len(items)} 个文件）")
        out.append("     症状：数据被写了两份，用户目录那一份之外的孤儿无人知晓")
        out.append(f"     内容：{items[:12]}")
    return out


def _data_home() -> str:
    """按服务端同一套逻辑解析用户数据目录。

    不能另写一份：两份实现一旦漂移，这里验的就不是用户真实的数据位置。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from omegaforge.core.paths import packaged_home  # noqa: PLC0415
    return packaged_home()


def _data_home_problem(install_dir: str,
                       home_existed_before: bool) -> list[str]:
    """卸载后用户数据必须还在，且不得位于安装目录内。"""
    try:
        home = _data_home()
    except Exception as exc:
        return [f"解析用户数据目录失败：{exc}",
                "     读不到目录不得当作通过，否则这条检查恒为空集"]

    inst = os.path.abspath(install_dir)
    home_abs = os.path.abspath(home)
    out: list[str] = []
    if home_abs == inst or home_abs.startswith(inst + os.sep):
        out.append(f"用户数据目录位于安装目录内：{home}")
        out.append("     症状：卸载整棵删除安装目录，用户数据随之消失")
    if not os.path.isdir(home_abs):
        # 只在卸载前确实存在过时才判"被删了"。数据目录从未建立说明前置
        # 步骤没有写入任何东西，那是另一回事，不该在这里伪装成数据丢失。
        if home_existed_before:
            out.append(f"卸载后用户数据目录已不存在：{home}")
            out.append("     症状：卸载把用户积累的数据一起删了")
    return out


def _wait_main_gone(names: list[str], install_dir: str,
                    timeout: float) -> bool:
    """等主程序真正消失，而不是只看卸载器的返回码。

    NSIS 卸载器不带 `_?=` 时会把自己复制到临时目录另起进程，父进程零点几秒
    就返回 0 —— 此刻卸载其实还没开始。只看返回码会把"尚未卸载"判成"卸载
    成功"，用户在界面上点了卸载、程序却还开着。故一律轮询到文件真的不在了
    为止；等不到必须判失败，不得放行。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        left = [n for n in names
                if ilc.find_file(install_dir, os.path.splitext(n)[0], ".exe")]
        if not left:
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: ci_verify_uninstall.py <安装目录> [端口]")
        return 2
    install_dir = sys.argv[1]

    names = _main_exe_names(install_dir)
    if not names:
        print("FAIL 读不到 productName，无法判定主程序是否残留")
        print("     读不到不能当作通过")
        return 1

    # 卸载器：NSIS 产物里的卸载入口。找不到就是用户无法卸载。
    uninstaller = None
    for cand in ("uninst.exe", "Uninstall.exe", "uninstall.exe"):
        p = os.path.join(install_dir, cand)
        if os.path.isfile(p):
            uninstaller = p
            break
    if uninstaller is None:
        print(f"FAIL 卸载入口不存在于安装目录：{install_dir}")
        print("     用户无法卸载 —— 这是产品缺陷，不是环境问题")
        return 1
    print(f"OK 卸载入口存在：{os.path.basename(uninstaller)}")

    # 卸载前先记住用户数据目录是否已经存在，用于区分"被卸载删掉"与
    # "压根没人写过数据"——后者不该伪装成数据丢失。
    home_existed = os.path.isdir(_data_home())
    print(f"卸载前用户数据目录：{_data_home()}（已存在={home_existed}）")

    # 带着正在运行的应用卸载：这是真实用户场景。
    port = ilc.free_port(int(sys.argv[2]) if len(sys.argv) > 2 else 8792)
    sidecar = ilc.find_file(install_dir, ilc.SIDECAR_PREFIX, ".exe")
    proc = None
    if sidecar:
        env = dict(os.environ, OF_PORT=str(port))
        proc = subprocess.Popen([sidecar], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        alive = _probe(port)
        print(f"卸载前服务状态：{'已监听 ' + str(port) if alive else '未就绪'}")
    else:
        print("卸载前服务状态：安装目录里没有可启动的服务，跳过")

    try:
        print(f"开始静默卸载（超时 {UNINSTALL_TIMEOUT}s）…")
        t0 = time.time()
        try:
            # `_?=` 让卸载器就地同步执行。不给这个参数时它会把自己复制到
            # 临时目录另起进程，父进程立刻返回 0 —— 返回码与"是否卸干净"
            # 无关，等它返回再查文件是查了个寂寞。
            rc = subprocess.run([uninstaller, "/S", f"_?={install_dir}"],
                                timeout=UNINSTALL_TIMEOUT).returncode
            elapsed = time.time() - t0
        except subprocess.TimeoutExpired:
            print(f"FAIL 卸载器超过 {UNINSTALL_TIMEOUT}s 未退出")
            print("     症状：卸载界面一直转，用户只能强杀")
            return 1
        print(f"卸载器退出码={rc} 用时={elapsed:.1f}s")

        # 主程序必须真的消失：返回码为 0 不等于已经卸掉
        if not _wait_main_gone(names, install_dir,
                               float(os.environ.get("OF_UNINSTALL_WAIT", "60"))):
            left = [n for n in names
                    if ilc.find_file(install_dir, os.path.splitext(n)[0], ".exe")]
            print(f"FAIL 卸载器已返回但主程序仍在：{left}")
            print("     症状：用户点了卸载、界面提示完成，程序却还开着")
            return 1
        print(f"OK 卸载后主程序已不存在（检查过 {len(names)} 种名字写法）")

        # 服务进程必须不再存活
        if proc is not None:
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(1)
            if proc.poll() is None:
                print("FAIL 卸载后服务进程仍存活")
                print("     症状：用户以为卸掉了，实际仍在占端口、仍在写数据")
                proc.kill()
                return 1
            print("OK 卸载后服务进程已终止")

        # 用户数据不得随卸载一起消失。数据若落在安装目录内，卸载会把整棵
        # 目录删掉——用户积累的知识库、待办、蒸馏产物一并消失，而卸载界面
        # 不会有任何提示。这里用与服务端相同的解析逻辑算出数据目录再查。
        bad_home = _data_home_problem(install_dir, home_existed)
        if bad_home:
            for b in bad_home:
                print(f"FAIL {b}")
            return 1
        print(f"OK 卸载后用户数据仍在：{_data_home()}")

        residual = sorted(os.listdir(install_dir)) if os.path.isdir(install_dir) else []
        print(f"卸载后安装目录残留条目 {len(residual)} 个：{residual[:12]}")
        bad = _runtime_residual(install_dir, residual)
        if bad:
            for b in bad:
                print(f"FAIL {b}")
            return 1
        print("OK 卸载后运行时已清除")
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
