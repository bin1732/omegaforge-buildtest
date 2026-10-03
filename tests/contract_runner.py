"""契约脚本的统一执行入口。

为什么需要这个模块
--------------------
仓库里有若干"脚本式"契约检查：它们以 `def main() -> int` +
`if __name__ == "__main__": sys.exit(main())` 结尾，**没有一个 `test_`
函数**。pytest 收集时因此拿到 **0 项**——它们既没被
`test_legacy_suites.py` 的 SUITES 列表登记，也不会被任何批次执行。

验证（本次改动定位）：以下三个文件在整仓回归里贡献 **0 项**：

  tests/test_frontend_contract.py     (43 项检查)
  tests/test_frontend_contract_guard.py  (13 项检查)
  tests/test_response_shape_probe.py    (接口形态探测)

也就是：我缺少该约束时报的"前端契约 43/43 通过"其实**从未真正执行过**——
数字来自记忆而非本次改动运行。这是"全绿是假的"最危险的一种形态：
不是断言写错，而是**整份检查压根没跑**。

更糟的是 `test_frontend_contract.py` 直接执行会崩：它在 setup 里写
`srv.RUNS.runs_dir = ...`，而 runs_dir 在单例惰性化改造后已是**只读
property**（core/run.py），赋值抛 AttributeError。也就是说这份契约
在改造之后就坏了，却因为没人执行而毫无动静。

本模块提供唯一实现，三个文件各自只加一行包装函数——避免"各起一套"。

为什么用子进程而不是直接调 main()
----------------------------------
1. 这些脚本会起真实 HTTP 服务并占用固定端口；子进程可完整隔离。
2. 子进程能验证"脚本作为整体能跑通"，而不是只验证某个内部函数——
  后者会漏掉 setup 阶段的崩溃（本项目已多次栽在"只测内层不测接通"）。
"""

from __future__ import annotations

import os
import subprocess
import sys


def run_contract_script(path: str, timeout: int = 300) -> None:
  """以子进程执行契约脚本，断言其成功退出。

  脚本自身负责打印与计数；这里只关心三件事：
   1. 退出码为 0（脚本内任何一项失败都应让它非 0）
   2. 输出里出现成功计数且**没有**失败计数
   3. 崩溃（traceback / AttributeError）一律视为失败
  """
  path = os.path.abspath(path)
  proc = subprocess.run(
    [sys.executable, path],
    capture_output=True, text=True, timeout=timeout,
    cwd=os.path.dirname(os.path.dirname(path)),
  )
  out = (proc.stdout or "") + (proc.stderr or "")
  tail = "\n".join(out.strip().splitlines()[-25:])

  assert proc.returncode == 0, (
    f"契约脚本 {os.path.basename(path)} 退出码 {proc.returncode}\n{tail}")

  # 崩溃特征：回归里出现 traceback 就算失败，即便退出码碰巧是 0
  for bad in ("Traceback (most recent call last)", "AttributeError",
        "can't set attribute"):
    assert bad not in out, (
      f"契约脚本 {os.path.basename(path)} 输出含崩溃特征 {bad!r}\n{tail}")

  # 有计数的脚本（"通过 N / 失败 M"）额外核失败数为 0
  if "通过" in out and "失败" in out:
    import re
    m = re.search(r"通过\s*(\d+)\s*/\s*失败\s*(\d+)", out)
    if m:
      assert m.group(2) == "0", (
        f"契约脚本 {os.path.basename(path)} 存在失败项："
        f"通过 {m.group(1)} / 失败 {m.group(2)}\n{tail}")
      assert int(m.group(1)) > 0, (
        f"契约脚本 {os.path.basename(path)} 未执行任何检查项\n{tail}")
