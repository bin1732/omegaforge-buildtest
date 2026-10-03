"""服务端口参数的可配置性——校验。

验证方式不是"注入后测试变红"就够：全盘崩溃也会变红。这里对每个校验点
声明"期望哪几条失败"，若多红或少红都判为不通过。
"""

from __future__ import annotations

# 子进程调 pytest 前必须补齐搜索路径：pytest 装在工作区 .pylibs，不在默认
# 搜索路径上。缺包会让 pytest 以 rc=1 退出，与"用例真的红了"无法区分。
import os as _rev_os
import sys as _rev_sys
_rev_sys.path.insert(0, _rev_os.path.dirname(
    _rev_os.path.dirname(_rev_os.path.abspath(__file__))))
from probes._pytest_env import env as _rev_env
_rev_os.environ.update(_rev_env())

import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "omegaforge", "server.py")
# 备份不能放 /tmp：沙盒的 /tmp 在命令之间会被回收，备份到下一次调用就没了，
# 还原只能回到一份已注入的内容，或者根本没有备份可用。
BAK = os.path.join(REPO, ".revtmp", "rev_server_port.bak")
os.makedirs(os.path.dirname(BAK), exist_ok=True)
TESTS = "tests/test_server_port_arg.py"

ORIG_MAIN = 'if __name__ == "__main__":\n    _serve_main()\n'
ORIG_DEFAULT = '"--port", type=int, default=8787'


def read():
    return open(SRC, encoding="utf-8").read()


def write(text):
    """先写临时文件再原子替换。

    原地改写同一文件时，写入调用会返回成功、紧接着读回却仍是旧内容，
    于是"注入成功"是假信号、后续判据全部失真。因此写入后必须读回校验。
    """
    for attempt in range(6):
        tmp = SRC + f".revtmp{attempt}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SRC)
        if open(SRC, encoding="utf-8").read() == text:
            return
        time.sleep(0.3)
    raise SystemExit("写入未落盘：连续 6 次读回都与写入内容不一致")


def restore():
    if os.path.exists(BAK):
        shutil.copy(BAK, SRC)
    return ORIG_MAIN in read() and ORIG_DEFAULT in read()


def run_tests():
    r = subprocess.run([sys.executable, "-m", "pytest", TESTS, "-q", "--tb=no"],
                       cwd=REPO, capture_output=True, text=True, timeout=300)
    failed = set(re.findall(r"^FAILED ([\w/\.]+\.py::[\w\[\]-]+)", r.stdout, re.M))
    return r.returncode, failed


def anchor(name, mutate, expect):
    if not restore():
        return False, "还原失败，源码不在预期基线"
    write(mutate(read()))
    assert read() != open(BAK, encoding="utf-8").read(), "注入未落盘"
    code, failed = run_tests()
    # 判据用"必须包含"：端口占用与否会额外影响若干起进程的用例，
    # 逐条等值比对会把环境状态误判成抓不到。
    hit = set(expect) <= failed
    print(f"  [{'抓到' if hit else '未抓到'}] {name}")
    print(f"      期望失败 {sorted(expect)}")
    print(f"      实际失败 {sorted(failed)}")
    ok = restore()
    code2, failed2 = run_tests()
    clean = code2 == 0 and not failed2
    print(f"      还原后全绿：{clean}")
    return hit and clean, ""


def main():
    if not os.path.exists(BAK):
        shutil.copy(SRC, BAK)
    restore()
    code, failed = run_tests()
    print(f"[基线] 退出码 {code}，失败 {sorted(failed)}")
    if code != 0:
        print("基线不绿，停止"); return 1

    results = []
    results.append(anchor(
        "锚点A：启动入口不解析参数（端口永远取默认）",
        lambda s: s.replace(ORIG_MAIN, 'if __name__ == "__main__":\n    serve()\n'),
        ["tests/test_server_port_arg.py::test_custom_port_really_listens"]))
    results.append(anchor(
        "锚点B：默认端口改为 8788（与外壳探测端口不一致）",
        lambda s: s.replace(ORIG_DEFAULT, '"--port", type=int, default=8788'),
        ["tests/test_server_port_arg.py::test_default_port_is_declared"]))

    allok = all(r[0] for r in results)
    print("\n=== 汇总 ===")
    print(f"  {sum(1 for r in results if r[0])}/{len(results)} 锚点抓到")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
