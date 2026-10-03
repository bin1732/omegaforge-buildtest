#!/usr/bin/env python3
"""后端未启动审计的元审计：确认场景前提缺失会被区分出来。

被验的脚本要验"后端不可达时的界面文案"，前提是那个地址上确实没有服务。
前提不成立时它会把场景问题记成产品缺陷；而随便起一个答非所问的服务时
断言又会全过——同一个前提缺失，两种相反的结果，排查时无从下手。

本脚本验的是"场景不成立会不会被当成产品失败"，三个校验点：

A 场景不成立   真起一个后端占住地址，审计必须报"场景不成立"，且不得出现
               任何 [FAIL] 行——否则场景问题又被记成了产品缺陷
B 前提判定失效 把前提判定改成恒真，场景不成立时必须重新出现 [FAIL] 行。
               这条是防 A 走形式：只验 A 的话，前提判定被摘掉后 A 依然
               全绿，而它恰恰是 A 唯一依赖的东西
C 基线         地址空闲时，审计必须全部通过

用法：python3 probes/rev_backend_down.py [A|B|C|all]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "probes", "frontend_render", "audit_backend_down.py")
BACKUP = "/tmp/rev_backend_down_target.py"

BE_PORT = os.environ.get("REV_BE_PORT", "8787")
HOME = "/tmp/rev_backend_down_home"

# B：让前提判定恒真，等价于把场景检查摘掉
ANCHOR_B_OLD = "        urllib.request.urlopen(BE_URL + \"api/runs\", timeout=3).read()"
ANCHOR_B_NEW = "        return True, \"\"\n"


def heal():
    """清掉遗留的注入痕迹与残留进程。

    注入与还原之间一旦被中断，目标文件会停在已注入状态，此后基线恒红、
    校验失去意义。因此每次开局都先无条件还原一次，让本脚本可重入。
    """
    if os.path.exists(BACKUP):
        shutil.copyfile(BACKUP, TARGET)
        os.remove(BACKUP)
        print("  [自愈] 还原遗留的注入")
    stop_backend()


def start_backend():
    """真起一个后端占住前端写死的地址。"""
    os.makedirs(HOME, exist_ok=True)
    env = dict(os.environ, OMEGAFORGE_HOME=HOME)
    p = subprocess.Popen(
        [sys.executable, "-m", "omegaforge.server", "--port", BE_PORT],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{BE_PORT}/api/runs", timeout=2).read()
            return p
        except Exception:
            if p.poll() is not None:
                raise SystemExit("后端进程未能启动")
            time.sleep(0.5)
    raise SystemExit("后端在 30 秒内未就绪")


def stop_backend(proc=None):
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    subprocess.run(["pkill", "-f", f"omegaforge.server --port {BE_PORT}"],
                   capture_output=True)
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{BE_PORT}/api/runs", timeout=1).read()
        except Exception:
            return
        time.sleep(0.5)


def inject(old, new):
    shutil.copyfile(TARGET, BACKUP)
    src = open(TARGET, encoding="utf-8").read()
    if old not in src:
        raise SystemExit(f"注入失败：找不到锚点（审计脚本已变）")
    open(TARGET, "w", encoding="utf-8").write(src.replace(old, new, 1))


def restore():
    if os.path.exists(BACKUP):
        shutil.copyfile(BACKUP, TARGET)
        os.remove(BACKUP)


def run_audit():
    env = dict(os.environ, FE_BE_URL=f"http://127.0.0.1:{BE_PORT}/")
    p = subprocess.run([sys.executable, TARGET], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=240)
    return p.returncode, p.stdout + p.stderr


def anchor_scene_broken():
    """A：场景不成立时必须报场景问题，且不记成产品失败。"""
    heal()
    be = start_backend()
    try:
        code, out = run_audit()
    finally:
        restore()
        stop_backend(be)
    if code == 0:
        return "后端在跑时审计仍判通过：场景检查没起作用"
    if "场景不成立" not in out:
        return f"审计判未通过，但没有说明是场景问题：{out[:160]!r}"
    if "[FAIL]" in out:
        n = out.count("[FAIL]")
        return f"场景问题被记成了 {n} 条产品失败：[FAIL] 行不应出现"
    return None


def anchor_guard_removed():
    """B：前提判定被摘掉后，场景不成立必须重新记成产品失败。"""
    heal()
    inject(ANCHOR_B_OLD, ANCHOR_B_NEW)
    be = start_backend()
    try:
        code, out = run_audit()
    finally:
        restore()
        stop_backend(be)
    if code == 0:
        return "前提判定失效后审计仍判通过：A 校验点失去意义"
    if "场景不成立" in out:
        return "前提判定已失效，却仍报场景不成立"
    if "[FAIL]" not in out:
        return "前提判定失效后没有任何 [FAIL] 行，A 校验点成了空转"
    return None


def anchor_baseline():
    """C：地址空闲时审计必须全部通过。"""
    heal()
    code, out = run_audit()
    if code != 0:
        tail = [l.strip() for l in out.splitlines() if l.strip()][-3:]
        return "基线未通过：" + " | ".join(tail)
    if "7/7" not in out:
        return f"基线通过，但断言数不是 7：{out[-160:]!r}"
    return None


ANCHORS = {
    "A": ("场景不成立", anchor_scene_broken),
    "B": ("前提判定失效", anchor_guard_removed),
    "C": ("基线", anchor_baseline),
}


def main():
    heal()
    want = sys.argv[1] if len(sys.argv) > 1 else "all"
    keys = list(ANCHORS) if want == "all" else [want.upper()]
    bad = []
    for k in keys:
        name, fn = ANCHORS[k]
        try:
            reason = fn()
        except subprocess.TimeoutExpired:
            reason = "审计超时（240 秒）"
        except SystemExit as e:
            reason = str(e)
        if reason:
            bad.append((name, reason))
            print(f"  [未抓到] {name}：{reason}")
        else:
            print(f"  [抓到] {name}")
    print(f"\n校验点 {len(keys)} 个，精确抓到 {len(keys) - len(bad)} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
