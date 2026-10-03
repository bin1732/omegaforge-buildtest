"""服务启动端口与地址参数的可配置性。

判据不读代码、只起真进程：传入端口后连一个真 HTTP 请求，连得上才算监听
生效。仅断言"源码里出现 --port 字样"属于字面量检查，改了参数名或忘了
接通都抓不到。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = [sys.executable, "-m", "omegaforge.server"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 等进程退出的时间预算。仅导入本模块并走到参数校验就要 1.6~3.0 秒，整批
# 跑时更长；预算给短了会把"退出得慢一点"判成"没有退出"，而后者正是这两
# 条用例要守的。轮询一发现进程结束就返回，故放宽预算只在失败时才付出时间。
_EXIT_BUDGET = 30.0


def _run(args, wait=_EXIT_BUDGET):
    """起进程并等它自己退出。

    固定 sleep 会把"退出得慢一点"误判成"没有退出"：判据应轮询到进程真的
    结束，而不是睡够一个拍脑袋的时长。
    """
    home = "/tmp/server_port_case"
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, OMEGAFORGE_HOME=home)
    p = subprocess.Popen(PROG + args, cwd=REPO, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + wait
    while time.time() < deadline and p.poll() is None:
        time.sleep(0.3)
    alive = p.poll() is None
    try:
        p.kill()
    except Exception:
        pass
    out, err = p.communicate(timeout=10)
    return alive, (out or ""), (err or "")


def test_custom_port_really_listens():
    port = _free_port()
    home = "/tmp/server_port_case"
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, OMEGAFORGE_HOME=home)
    p = subprocess.Popen(PROG + ["--port", str(port)], cwd=REPO, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ok = False
        # 等待窗口要覆盖"整批跑、机器负载高"的情形：写死十秒时，前序用例
        # 留下的负载会让服务来不及起来，用例红灯而原因与端口参数无关。
        for _ in range(60):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=2).read()
                ok = True
                break
            except Exception:
                continue
        if not ok:
            # 只报"没在监听"会把两种相反的原因合成一条：服务起得慢，和
            # 服务根本没起来（端口冲突、参数没接上）。后者才是本用例要守
            # 的，故失败时必须把服务自己的输出带出来。
            try:
                p.kill()
            except Exception:
                pass
            out, err = p.communicate(timeout=10)
            tail = ((out or "") + (err or ""))[-800:]
            assert False, (f"指定 --port {port} 后该端口并没有在监听；"
                           f"服务输出：{tail!r}")
    finally:
        p.kill()
        p.communicate(timeout=10)


def test_default_port_is_declared():
    """默认端口仍须是 8787：桌面外壳按该端口探测就绪。"""
    src = open(os.path.join(REPO, "omegaforge", "server.py"), encoding="utf-8").read()
    m = re.search(r'--port",\s*type=int,\s*default=(\d+)', src)
    assert m, "未找到 --port 的默认值声明"
    assert int(m.group(1)) == 8787, f"默认端口应为 8787，实际 {m.group(1)}"


@pytest.mark.parametrize("value", ["abc", "70000", "0"])
def test_bad_port_rejected_in_chinese(value):
    alive, out, err = _run(["--port", value])
    text = (out or "") + (err or "")
    assert not alive, f"--port {value} 非法，服务不该启动"
    assert "参数有误" in text, f"非法端口未给出中文提示：{text[:120]!r}"
    # 只比对命令行层的英文模板：按"不含任何字母串"来判会误伤程序名本身
    for eng in ("invalid int value", "unrecognized arguments",
                "the following arguments", "Traceback"):
        assert eng not in text, f"报错里残留英文模板：{eng}"


def test_help_is_chinese():
    _, out, err = _run(["--help"])
    text = (out or "") + (err or "")
    assert "--port" in text, "help 未列出 --port"
    assert "用法：" in text, "help 的 usage 前缀未中文化"
    for eng in ("usage:", "options:", "positional arguments"):
        assert eng not in text.lower(), f"help 里残留英文小节：{eng}"
