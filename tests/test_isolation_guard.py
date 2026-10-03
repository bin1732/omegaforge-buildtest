"""测试基建自身的守卫：全局单例不允许跨用例泄漏。

为什么要有这个文件：RUNS / CONVS / USAGE 都是 server 的模块级单例。
某个用例改了 `srv.CONVS.home = tmp` 却不还原，后续用例就会静默跑在
**已被 rmtree 删除的目录**上，表现为 FileNotFoundError 或莫名其妙的
断言失败——但单独跑那个文件却全绿，极难定位。

缺少该约束时这套污染已造成过：
 · 会话写入已删目录 → FileNotFoundError（产品其实是对的）
 · HTTP 服务端口不释放 → 后续套件 "Address already in use"
 · 整套 pytest 从"几十秒"退化到"跑十几分钟仍不结束"

conftest._restore_server_globals 负责还原；本文件负责**证明它有效**。
两个用例必须按定义的先后顺序执行（pytest 默认即文件内顺序）。
"""

from __future__ import annotations

import os

POISON = "/tmp/of_poison_dir_that_must_not_persist"


def test_a_poisons_globals() -> None:
  """故意泄漏：改全局单例与环境变量，且不还原。"""
  import omegaforge.server as srv

  # dir / runs_dir 已是只读 property，从 home 现算（core/paths.py），
  # 只改 home 即可把整个单例导向 POISON。
  srv.CONVS.home = POISON
  srv.RUNS.home = POISON
  os.environ["OMEGAFORGE_HOME"] = POISON


def test_b_survives_the_poison() -> None:
  """上一个用例泄漏的状态必须已被还原，否则这里会失败或卡死。"""
  import omegaforge.server as srv

  assert srv.CONVS.home != POISON, "CONVS.home 泄漏到了下一个用例"
  assert srv.RUNS.home != POISON, "RUNS.home 泄漏到了下一个用例"
  assert os.environ.get("OMEGAFORGE_HOME") != POISON, (
    "OMEGAFORGE_HOME 泄漏到了下一个用例")

  # 光看变量不够——泄漏最典型的后果是"写不进去"，所以真写一次
  conv = srv.CONVS.new("隔离探测脚本")
  assert conv["title"] == "隔离探测脚本"
  assert os.path.isdir(srv.CONVS.dir), (
    f"还原后的会话目录不可用：{srv.CONVS.dir}")
