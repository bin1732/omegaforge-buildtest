"""pytest 收集配置。

四个「独立审计套件」风格的脚本已改名为 `_legacy_*.py`（不再匹配 `test_*.py`）：
它们模块顶层直接执行全部用例并在末尾 sys.exit()，设计用途是
`python3 tests/_legacy_xxx.py` 独立运行。

为什么必须靠**改名**而不是 collect_ignore
------------------------------------------
原先靠 `collect_ignore` 排除，但它【只在目录递归时生效】——命令行上显式点名
文件时 pytest 照样收集、照样 import。验证后果：

  pytest tests/       全绿（sse_body 被忽略）
  pytest tests/test_s*.py  必红（shell 通配把 sse_body 显式点名进来）

而 `_legacy_sse_body.py` 被 import 时会在【收集阶段】启动两个 HTTP 服务、
写 providers.json、并把 OMEGAFORGE_HOME 劫持到自己的临时目录；后续
`test_server_compare_runs.py::test_real_distill_persists_fingerprint` 于是
拿到被改写的 provider 配置，真实蒸馏在 extract 阶段拿到非 JSON 内容而失败。

这是「全绿取决于怎么调用 pytest」的又一种形态——`__pycache__` 里存在
这四个模块的 pyc，证明该隐患缺少该约束时真实发生过。改成 `_legacy_` 前缀后，
任何 `test_*.py` 通配都捡不到它们，与调用方式无关。
"""

import os
import tempfile

import pytest

# --- 全局测试 HOME：唯一允许在 import 期改环境的地方 --------------------------
#
# 为什么放在 conftest（而不是每个测试文件各写一行 setdefault）：
#  发现有 6 个测试文件在模块顶层写 os.environ，其中
#   os.environ.setdefault("OMEGAFORGE_HOME", "/tmp/omegaforge_mcp_limits_home")
#  这类语句的结果是——**谁先被 import，整个 pytest 会话的 HOME 就是谁的**。
#  import 顺序由文件名字母序决定，于是"数据落在哪个目录"取决于文件名，
#  而不是取决于用例想要什么。这正是"单独跑全绿、整批跑必红"的温床。
#
#  更实际的后果：HOME 未设时默认值是 `os.getcwd()/.omegaforge`，即**仓库
#  根目录**——测试会往源码树里写数据。
#
# conftest.py 由 pytest 最先 import，因此在这里钉一次，后续所有测试模块
# import 时看到的已是隔离目录。这是集中式、可审计的单点；其它测试文件
# 一律禁止再写（由 tests/test_import_purity.py 守卫）。
if not os.environ.get("OMEGAFORGE_HOME"):
  os.environ["OMEGAFORGE_HOME"] = tempfile.mkdtemp(prefix="of_pytest_home_")


@pytest.fixture(autouse=True)
def _restore_server_globals():
  """每个用例前后还原 server 模块的全局单例与数据目录环境变量。

  为什么必须做：RUNS / CONVS / USAGE / TASKS 等都是【模块级单例】，
  测试里改 `srv.RUNS.home = tmp` 只改了这一个属性，而环境变了、目录被
  rmtree 之后，**后续用例会静默跑在已被删除的路径上**——表现为
  FileNotFoundError 或"断言莫名其妙失败"，但单独跑同一个文件却全绿。

  这类污染缺少该约束时已踩多次（写入已删目录、端口占用、home 残留）。
  与其在每个套件里手写快照还原（容易漏），不如在这里通用兜底：
  快照所有大写全局对象的 __dict__，用例结束后整体还原。
  """
  try:
    import omegaforge.server as srv
  except Exception:
    yield
    return

  snaps = {}
  for name in dir(srv):
    if not name.isupper():
      continue
    obj = getattr(srv, name, None)
    if obj is None or isinstance(obj, type):
      continue
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
      try:
        snaps[name] = dict(d)
      except Exception:
        pass

  prev_env = os.environ.get("OMEGAFORGE_HOME")

  yield

  for name, snap in snaps.items():
    obj = getattr(srv, name, None)
    d = getattr(obj, "__dict__", None) if obj is not None else None
    if isinstance(d, dict):
      d.clear()
      d.update(snap)

  if prev_env is None:
    os.environ.pop("OMEGAFORGE_HOME", None)
  else:
    os.environ["OMEGAFORGE_HOME"] = prev_env
