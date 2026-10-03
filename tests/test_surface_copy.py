"""test_surface_copy — 表层文案守卫。

为什么需要这个测试（每一条都来自验证，不是理论）：

  本项目反复出现"英文技术文案直接进界面"的问题，且**处理过之后仍会回归**：
   · {"error": "unknown job"}     —— HTTP 404 响应体，前端直接上屏
   · {"error": "f{fname} not ready"}  —— 把内部文件名暴露给用户
   · {"error": f"read {fname} failed: {e}"} —— 异常原文 + 路径，最严重
   · answer="(max steps reached)"   —— agent 回答位置出现英文技术文本
   · [simulated xxx result for: ...]  —— mock 标记混进对话内容

  这些字符串散落在业务代码里，靠人眼 review 必然漏。
  本测试用 AST 精确提取"用户可见位置"的字符串常量，判定是否为未审校英文，
  把规范固化成 CI 闸门：新增英文文案 → 构建失败。

判定口径（重要）：
  只检查**用户可见**的位置：
   1. self._json({"error": ...}) 里 error 键对应的字面量
   2. TaskResult(answer=...) 等 answer 关键字参数的字面量
  不检查：日志、注释、docstring、变量名、异常类名——那些本来就是给开发者看的。

豁免清单（JSON-RPC 协议强制文本，改了会破坏协议兼容性）：
  mcp_server.py 的 "method not found: ..." 属于 JSON-RPC 2.0 标准错误，
  MCP 客户端依赖它做方法分派，必须保留原文，因此该文件整体豁免 error 检查。
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "omegaforge"

# JSON-RPC 2.0 协议文本，不得本地化
PROTOCOL_EXEMPT_FILES = {"mcp_server.py"}

# 已知合规的英文：技术标识符、品牌名、型号、单位等，允许出现在用户可见文案里
ALLOWED_TOKENS = {
  "ok", "id", "url", "api", "json", "http", "https", "mcp", "llm", "token",
  "openai", "gpu", "cpu", "sse", "utf", "ascii", "pdf", "csv", "kb", "mb",
  "omegaforge", "studio", "arena", "genome", "skill", "agent",
}

# 判定"这是给用户看的英文"：含空格的纯 ASCII 短语，或明显的技术错误词
_ENGLISH_PHRASE = re.compile(r"^[A-Za-z0-9_\-\.\s\(\)\[\]:/]{3,}$")
_ERROR_WORDS = re.compile(
  r"\b(error|failed|invalid|missing|not found|unknown|required|"
  r"exception|denied|timeout|refused|unexpected|simulated)\b",
  re.I,
)


def _is_chinese(s: str) -> bool:
  return any("一" <= ch <= "鿿" for ch in s)


def _looks_unreviewed_english(s: str) -> bool:
  """判定一个用户可见字符串是否为未审校的英文技术文案。"""
  if not s or _is_chinese(s):
    return False
  # 纯技术标识符（无空格、无错误词）视为合规：如 "max_steps"、"genome.json"
  if " " not in s and not _ERROR_WORDS.search(s):
    return False
  # 命中白名单技术词的短语（如 "model not ready" 里 ready 不在黑名单）仍要查错误词
  if _ERROR_WORDS.search(s):
    return True
  # 含空格的纯 ASCII 短语，且不是全白名单词 → 疑似未翻译
  words = {w.strip("().[]:/").lower() for w in s.split()}
  if words and words.issubset(ALLOWED_TOKENS):
    return False
  return bool(_ENGLISH_PHRASE.match(s)) and " " in s


def _error_literals(tree: ast.AST) -> list[tuple[int, str]]:
  """提取 _json({"error": <字面量>}) 中 error 键的字面量字符串。"""
  found: list[tuple[int, str]] = []
  for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
      continue
    # 只看第一个位置参数是 Dict 的调用（self._json({...}, code)）
    if not node.args or not isinstance(node.args[0], ast.Dict):
      continue
    for k, v in zip(node.args[0].keys, node.args[0].values):
      if isinstance(k, ast.Constant) and k.value == "error":
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
          found.append((v.lineno, v.value))
        elif isinstance(v, ast.JoinedStr):
          # f-string：取其中所有常量片段拼接成模板骨架来判定
          parts = [
            p.value for p in v.values
            if isinstance(p, ast.Constant) and isinstance(p.value, str)
          ]
          if parts:
            found.append((v.lineno, "".join(parts)))
  return found


def _kwarg_literals(tree: ast.AST, kw: str) -> list[tuple[int, str]]:
  """提取 f(answer=<字面量>) 形式的字面量。"""
  found: list[tuple[int, str]] = []
  for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
      continue
    for k in node.keywords:
      if k.arg == kw and isinstance(k.value, ast.Constant):
        if isinstance(k.value.value, str):
          found.append((k.value.lineno, k.value.value))
  return found


class TestSurfaceCopy(unittest.TestCase):
  def test_no_unreviewed_english_in_error_payloads(self):
    """HTTP 响应体里的 error 字段不得出现未审校英文。"""
    bad: list[str] = []
    for py in sorted(PKG.rglob("*.py")):
      if py.name in PROTOCOL_EXEMPT_FILES:
        continue
      try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
      except SyntaxError as e: # pragma: no cover - 语法错误由编译阶段拦截
        self.fail(f"{py}: 语法错误 {e}")
      for lineno, text in _error_literals(tree):
        if _looks_unreviewed_english(text):
          bad.append(f"{py.relative_to(ROOT)}:{lineno} {text!r}")
    self.assertEqual([], bad, "用户可见的 error 文案存在未审校英文：\n" + "\n".join(bad))

  def test_no_unreviewed_english_in_agent_answers(self):
    """TaskResult(answer=...) 直接上屏，不得是英文技术文本。"""
    bad: list[str] = []
    for py in sorted(PKG.rglob("*.py")):
      if py.name in PROTOCOL_EXEMPT_FILES:
        continue
      tree = ast.parse(py.read_text(encoding="utf-8"))
      for lineno, text in _kwarg_literals(tree, "answer"):
        if _looks_unreviewed_english(text):
          bad.append(f"{py.relative_to(ROOT)}:{lineno} {text!r}")
    self.assertEqual([], bad, "agent 回答位置存在未审校英文：\n" + "\n".join(bad))

  def test_mcp_protocol_error_codes_preserved(self):
    """回退校验：MCP/JSON-RPC 兼容性由错误码保证，不是由英文文案保证。

    这条守卫原先断言源码里必须出现 "method not found" 这个英文串，
    等于把"协议契约"和"展示文案"绑死了。实际上 JSON-RPC 2.0 里
    message 是自由文本，客户端只看 code（-32601 等）判断错误类型；
    把文案中文化不会破坏任何客户端，反而符合"不把英文技术原文
    甩给用户/模型"的口径。改为锁错误码：谁改了 code，才是真的
    破坏了兼容性——这才是这条守卫该守住的东西。
    """
    import re
    p = PKG / "mcp_server.py"
    if not p.exists():
      self.skipTest("mcp_server.py 不存在")
    src = p.read_text(encoding="utf-8")
    for const, code in [("E_PARSE", "-32700"),
              ("E_INVALID_REQUEST", "-32600"),
              ("E_METHOD_NOT_FOUND", "-32601"),
              ("E_INVALID_PARAMS", "-32602")]:
      self.assertTrue(
        re.search(rf"^{const} = {code}$", src, re.M) is not None,
        f"{const} 必须等于标准码 {code}，改了会破坏 MCP 客户端兼容性")
    # 定义了不用等于没定义：未知方法分支必须真的引用该常量
    body = src.split("E_METHOD_NOT_FOUND = -32601", 1)[-1]
    self.assertIn("E_METHOD_NOT_FOUND", body, "错误码必须被实际使用")

  def test_guard_actually_detects(self):
    """自检：把英文文案喂给判定函数，必须被抓到——证明守卫不是形式化。"""
    for s in ["unknown job", "read failed", "not found", "(max steps reached)"]:
      self.assertTrue(_looks_unreviewed_english(s), f"守卫漏判: {s!r}")
    for s in ["未找到该任务", "操作失败，请稍后重试", "max_steps", "genome.json"]:
      self.assertFalse(_looks_unreviewed_english(s), f"守卫误报: {s!r}")


if __name__ == "__main__":
  unittest.main(verbosity=2)
