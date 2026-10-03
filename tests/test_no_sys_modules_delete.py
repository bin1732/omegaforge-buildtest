"""守卫：测试代码禁止用 `del sys.modules[...]` 重建 omegaforge。

为什么必须有一条静态守卫
--------------------------
这是本项目**第三次**栽在同一形态上，而且每次症状都是「单独跑全绿、
整批跑失败」——最容易被误判成"偶发/环境问题"而放过：

 1. 早期：某套件改了 OMEGAFORGE_HOME 不还原，污染后续文件
 2. 上一次改动：test_memory_security.py 用 `del sys.modules` 重建单例
   → test_mcp_limits.py 2 项、test_mcp_protocol.py 2 项在整批跑时红
 3. 本次改动：test_api_surface_guard.py 同一写法（已一并清理）

根因不是"忘了还原"，而是**类身份分裂**：

  del sys.modules["omegaforge*"] → 下次 import 时模块被**重新执行**
  → 同一个类产生两个不同的类对象
  → 别的测试文件在模块级 `from omegaforge.core.errors import UserError`
   绑定的是**旧对象**，运行时抛出的是**新对象**
  → assertRaises(UserError) 捕获不到；patch 打在新对象上、生效在旧对象上

单例已惰性化（`core/paths.py` 的 LazyHome）并暴露 `invalidate()`，
「换 home 重读」这件事有了正确做法，不需要再动 sys.modules。

本守卫做两件事：
 1. 扫描 tests/ 与 conftest.py，禁止再出现删除 omegaforge 模块的写法
 2. 确认 `invalidate()` 这个替代手段确实存在于所有惰性单例上
   —— 否则本守卫等于"只禁止、不给路"，下次必然有人绕过
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FORBIDDEN = re.compile(
  r"del\s+sys\.modules\s*\["     # del sys.modules[...]
  r"|del\s+sys\.modules\s*\[\s*m\s*\]"
)

# sys.modules.pop 同理（同样是"重建模块"的写法）
FORBIDDEN_POP = re.compile(r"sys\.modules\.pop\s*\(")

SKIP_FILES = {os.path.basename(__file__)}   # 本文件要贴出正则，不能自罚


def _scan_files():
  out = []
  for name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
    if not name.endswith(".py") or name in SKIP_FILES:
      continue
    out.append(os.path.join(ROOT, "tests", name))
  conftest = os.path.join(ROOT, "conftest.py")
  if os.path.isfile(conftest):
    out.append(conftest)
  return out


def _code_lines(path: str) -> dict:
  """返回 {行号: 该行代码文本}，注释与文档字符串被剔除。

  为什么必须剔除注释（本次改动验证踩到的坑）：
  我第一版直接按行做正则，结果把 test_api_surface_guard.py 与
  test_memory_security.py **docstring 里的反面示例**
  「为什么不再用 del sys.modules[...]」判成了违规——守卫自己误报。

  这与本项目已记录的一处教训同源但又相反：那次是"注释里写了
  errors="replace" 字面量，导致回退校验命中的是注释而不是代码"；
  这次是"注释里写反面教材，导致静态守卫命中注释而不是代码"。
  结论：**文本扫描必须区分代码与注释**，用 tokenize 按 token 类型取。
  """
  import io
  import tokenize

  src = open(path, encoding="utf-8").read()
  src_lines = src.splitlines()
  # 按**原始列位置**把非注释/字符串的 token 填回空白画布。
  # 为什么不能用 `" ".join(tok.string)` 重建（回退校验踩到）：
  # token 之间硬塞空格会把 `sys.modules` 拆成 `sys .modules`，正则
  # `sys\.modules` 再也匹配不到 —— 注入 `del sys.modules["omegaforge"]`
  # 的回退校验因此"未抓到"，险些让这条守卫变成无效守卫。
  canvas = {i: [" "] * len(l) for i, l in enumerate(src_lines, 1)}
  skip = {tokenize.COMMENT, tokenize.STRING, tokenize.NL,
      tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
      tokenize.ENDMARKER}
  with io.StringIO(src) as fh:
    for tok in tokenize.generate_tokens(fh.readline):
      if tok.type in skip:
        continue
      r0, c0 = tok.start
      r1, c1 = tok.end
      if r0 != r1:            # 跨行 token：只取首行片段
        c1 = len(src_lines[r0 - 1])
      row = canvas.get(r0)
      if row is None:
        continue
      for k in range(c0, min(c1, len(row))):
        row[k] = tok.string[k - c0]
  return {r: "".join(v).strip() for r, v in canvas.items()
      if "".join(v).strip()}


def test_no_test_file_deletes_omegaforge_modules():
  """核心守卫：任何测试文件的**代码**都不许删除 omegaforge 模块。"""
  offenders = []
  for path in _scan_files():
    for lineno, code in sorted(_code_lines(path).items()):
      if FORBIDDEN.search(code) or FORBIDDEN_POP.search(code):
        offenders.append(
          f"{os.path.basename(path)}:{lineno}: {code.strip()}")
  assert not offenders, (
    "发现删除 sys.modules 的写法（会导致类身份分裂，"
    "表现为「单独跑全绿、整批跑失败」）：\n " + "\n ".join(offenders)
    + "\n正确做法：调单例的 invalidate()，见 core/paths.py 的 LazyHome。"
  )


def test_invalidate_exists_as_the_supported_alternative():
  """只禁止不给路等于逼人绕过：确认 invalidate() 真的可用。"""
  from omegaforge import server as S

  lazy_singletons = [S.KB, S.WIKI, S.TASKS, S.USAGE, S.PROVIDERS, S.CONVS]
  for s in lazy_singletons:
    assert hasattr(s, "invalidate"), (
      f"{type(s).__name__} 缺少 invalidate()——禁止 del sys.modules 后"
      f"就没有换 home 重读的手段了"
    )
    assert callable(s.invalidate)

  from omegaforge.core.paths import LazyHome
  assert hasattr(LazyHome, "invalidate")
  assert hasattr(LazyHome, "ensure_loaded")


def test_invalidate_actually_forces_reload(tmp_path, monkeypatch):
  """invalidate() 必须真的导致重读，而不是只改个标记。

  回退校验点：把 LazyHome.invalidate 改成空实现（pass），
  本用例应变红——证明它测的是"重读"这件事本身。
  """
  monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))
  from omegaforge import server as S

  S.KB.add("标题", "中文内容")     # 第一次加载
  n1 = len(S.KB.all())
  assert n1 == 1

  # 外部直接改写文件，模拟"历史脏数据"或"手工改动"
  kb_file = os.path.join(str(tmp_path), "kb.json")
  import json as _json
  with open(kb_file, "w", encoding="utf-8") as f:
    _json.dump({"x": {"id": "x", "type": "note", "title": "外部写入",
             "text": "内容", "tags": [], "ts": 1}}, f)

  S.KB.invalidate()
  records = S.KB.all()
  assert len(records) == 1
  assert records[0].get("title") == "外部写入", (
    f"invalidate() 后未重读，仍是旧内容：{[r.get('title') for r in records]}"
  )


def test_reload_clears_cache_when_file_absent(tmp_path, monkeypatch):
  """换到空目录时，带内存缓存的单例必须清空——不能残留旧 home 的数据。

  验证（本次改动新增守卫时才暴露出来的真 bug）：
   KB._reload 在 `os.path.exists(self.path)` 为假时**不清空** self._docs，
   于是切到全新的空 tmp 目录后 all() 仍返回 8 条上一目录的残留，
   一旦 _save() 就会整份写进新目录。

   Tasks._reload 有 `self._items = {}` 这一句，KB 没有——同族实现只有
   一个成员漏，与 as_float/as_int、sed -i 是同一形态。
  """
  monkeypatch.setenv("OMEGAFORGE_HOME", str(tmp_path))
  from omegaforge import server as S

  S.KB.add("旧目录的条目", "内容")
  assert len(S.KB.all()) == 1

  # 切到另一个全新的空目录
  empty = tmp_path / "empty_home"
  empty.mkdir()
  monkeypatch.setenv("OMEGAFORGE_HOME", str(empty))

  assert S.KB.all() == [], (
    f"切到空目录后仍残留旧数据：{[d.get('title') for d in S.KB.all()]}"
  )
  assert S.TASKS.list() == [], "切到空目录后 Tasks 残留旧数据"


def test_run_store_invalidate_exists():
  """RunStore 不在 LazyHome 继承列表里，单独确认它有等价手段。"""
  from omegaforge import server as S
  assert hasattr(S.RUNS, "invalidate") or hasattr(S.RUNS, "home"), (
    "RunStore 既无 invalidate 也无 home，无法按环境重定位"
  )


# ---------------------------------------------------------------------------
# REVERT ANCHORS
# ---------------------------------------------------------------------------
# A. 把 LazyHome.invalidate 改成 `pass`（只改标记不重读）
#  → test_invalidate_actually_forces_reload 变红
# B. 删除 LazyHome.invalidate 方法
#  → test_invalidate_exists_as_the_supported_alternative 变红
# C. 在任一测试文件里加一行 `del sys.modules["omegaforge"]`
#  → test_no_test_file_deletes_omegaforge_modules 变红
