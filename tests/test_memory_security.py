"""记忆 / 知识库 / Wiki / 技能的边界与越界防护（全部由可复现驱动）。

本文件里的每一条断言，都对应一个**真实复现过**的故障，不是理论推演：

1. do_GET 无顶层兜底 —— 知识库存了类型非法的 tags 后，
  `/api/kb/search` 抛 TypeError 穿透 handler，服务端线程崩、
  连接被掐断且**不返回任何响应**，客户端收到 RemoteDisconnected。
  用户侧表现为"一搜索就转圈/网络错误"，而后端日志里连错误码都没有。
2. Wiki 越界读 —— `GET /api/wiki/page?slug=../secret` 验证返回 200
  并带出 wiki 目录之外的文件内容（save 校验了 slug，get/delete 没有）。
3. SkillManager 越界 —— name 未规整，可拼出技能目录之外的路径。

每条用例都同时验证"该拦的拦住"和"正常的仍然能用"，避免守卫把功能一起锁死。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)


_ENV_BEFORE: dict = {}


def _reload_singletons():
  """丢弃九个单例的内存缓存，下次访问时按当前环境重读。

  为什么不能再用 `del sys.modules["omegaforge*"]`（注意事项）：
  删除模块会让 omegaforge 被**重新导入**，于是同一个类产生两个不同的
  类对象。而 `tests/test_mcp_limits.py` 在**模块级**就
  `from omegaforge.core.errors import UserError`，绑定了旧对象；运行时
  抛出的是新对象 —— `assertRaises(UserError)` 因此永远捕获不到，
  表现为「单独跑全绿、整批跑失败」（4 项红）。

  单例已惰性化（`core/paths.py` 的 LazyHome），路径会跟随环境，
  所以这里只需要 `invalidate()` 让它们重读文件即可。
  """
  from omegaforge import server as _S
  for s in (_S.KB, _S.WIKI, _S.TASKS, _S.USAGE, _S.PROVIDERS, _S.CONVS, _S.RUNS):
    inv = getattr(s, "invalidate", None)
    if inv is not None:
      inv()


def _fresh_home():
  """每个用例一份独立 OMEGAFORGE_HOME，并让单例按新目录重读。"""
  home = tempfile.mkdtemp(prefix="ofsec_")
  for k in ("OMEGAFORGE_HOME", "OMEGAFORGE_MOCK"):
    if k not in _ENV_BEFORE:
      _ENV_BEFORE[k] = os.environ.get(k)
  os.environ["OMEGAFORGE_HOME"] = home
  os.environ["OMEGAFORGE_MOCK"] = "1"
  _reload_singletons()
  return home


def tearDownModule():
  """还原 _fresh_home 留下的全局污染。

  验证：本文件跑完后 `OMEGAFORGE_MOCK=1` 与临时 HOME 会残留到后续测试
  文件，导致 tests/test_mcp_limits.py 的 2 项在整批运行时失败、单独运行
  却全过——典型的"测试间互相污染"。环境变量必须还原，单例缓存也必须
  失效，否则后续文件会继承本文件的 HOME 与 mock 开关。
  """
  for k, v in _ENV_BEFORE.items():
    if v is None:
      os.environ.pop(k, None)
    else:
      os.environ[k] = v
  _reload_singletons()


class _Server:
  """起一个真实 HTTP 服务（端口由系统分配，避免并行跑测试时端口冲突）。"""

  def __init__(self):
    from omegaforge import server as S
    self.S = S
    self.srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    self.port = self.srv.server_address[1]
    threading.Thread(target=self.srv.serve_forever, daemon=True).start()

  def req(self, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
      f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
      headers={"Content-Type": "application/json"})
    try:
      with urllib.request.urlopen(r, timeout=10) as f:
        return f.status, f.read().decode()
    except urllib.error.HTTPError as e:
      return e.code, e.read().decode()
    except Exception as e:            # 连接被掐断等网络级故障
      return None, f"{type(e).__name__}: {e}"

  def close(self):
    self.srv.shutdown()
    self.srv.server_close()


class TestGetHandlerNeverDropsConnection(unittest.TestCase):
  """do_GET 的顶层兜底：任何异常都必须变成一条 JSON，绝不掐断连接。"""

  def setUp(self):
    _fresh_home()
    self.s = _Server()

  def tearDown(self):
    self.s.close()

  def test_illegal_tags_do_not_break_search(self):
    # 契约分两层，缺一不可：
    # ① 入口层：新增数据不得写入非法 tags（否则脏数据会持续累积）
    code, _ = self.s.req("POST", "/api/kb/add",
               {"title": "标题", "text": "中文内容测试",
               "tags": 123})
    self.assertEqual(code, 400, "入口层未拦住非法 tags")
    # ② 读取层：入口校验上线前已落盘的历史脏数据必须仍能检索，
    #  绝不允许 join(int) 抛 TypeError 把连接掐断（缺少该约束时 code 为 None）。
    home = os.environ["OMEGAFORGE_HOME"]
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "kb.json"), "w", encoding="utf-8") as f:
      json.dump({"a": {"id": "a", "type": "note", "title": "标题",
               "text": "中文内容测试", "tags": 123,
               "ts": 1}}, f)
    _reload_singletons()
    self.s.close()
    self.s = _Server()
    code, body = self.s.req("GET", "/api/kb/search?q=%E4%B8%AD%E6%96%87")
    self.assertIsNotNone(code, f"连接被掐断：{body}")
    self.assertEqual(code, 200)
    payload = json.loads(body)
    self.assertIn("results", payload)
    self.assertTrue(payload["results"], "历史脏数据应仍可被检索到")

  def test_illegal_tags_do_not_break_recall(self):
    self.s.req("POST", "/api/memory/remember",
          {"fact": "中文记忆内容", "tags": 999})
    code, body = self.s.req("GET", "/api/memory/recall?q=%E4%B8%AD%E6%96%87")
    self.assertIsNotNone(code, f"连接被掐断：{body}")
    self.assertEqual(code, 200)
    self.assertIn("memories", json.loads(body))

  def test_legal_tags_still_searchable(self):
    self.s.req("POST", "/api/kb/add",
          {"title": "部署手册", "text": "灰度发布步骤",
          "tags": ["ops", "deploy"]})
    code, body = self.s.req("GET", "/api/kb/search?q=%E7%81%B0%E5%BA%A6")
    self.assertEqual(code, 200)
    res = json.loads(body)["results"]
    self.assertTrue(res, "合法 tags 的正常检索不应被守卫误伤")
    # 规整后 tags 必须是字符串列表，不应被拆成单字
    self.assertEqual(res[0]["id"] and "ops", "ops")

  def test_unexpected_error_still_returns_json_not_dropped_connection(self):
    """直接打 do_GET 兜底本身：制造一个必然抛异常的 GET 请求。

    用例经历过两轮换脏数据（tags 非列表 → text 非字符串），因为它们
    先后被 _clean_tags / _safe_str 收敛掉，再也触发不到异常——**靠脏数据
    触发异常的用例会随修复一起失效**，这正是必须直接注入异常的理由。
    这里直接在 KB.search 上挂一个必抛的桩，专测"任何异常都变成 JSON"。
    """
    _reload_singletons()
    self.s.close()
    self.s = _Server()

    def boom(*_a, **_k):
      raise RuntimeError("injected-by-guard")

    self.s.S.KB.search = boom
    code, body = self.s.req("GET", "/api/kb/search?q=%E4%B8%AD%E6%96%87")
    # 缺少该约束时：code 为 None（连接被掐断，客户端只看到网络错误）
    self.assertIsNotNone(code, f"连接被掐断，未返回任何响应：{body}")
    self.assertEqual(code, 500)
    payload = json.loads(body)
    self.assertIn("error", payload)
    # 兜底响应必须是中文、且不得夹带异常类名 / traceback
    self.assertNotIn("TypeError", body)
    self.assertNotIn("Traceback", body)

  def test_corrupt_kb_file_does_not_break_list(self):
    home = os.environ["OMEGAFORGE_HOME"]
    with open(os.path.join(home, "kb.json"), "w", encoding="utf-8") as f:
      f.write('["not", "a", "dict"]')    # 手改坏的历史文件
    _reload_singletons()
    self.s.close()
    self.s = _Server()
    code, body = self.s.req("GET", "/api/kb/list")
    self.assertIsNotNone(code, f"连接被掐断：{body}")
    self.assertEqual(code, 200)


class TestWikiNoPathTraversal(unittest.TestCase):
  """Wiki 读写删三条路径统一卡在 _path，slug 非法即拒绝。"""

  def setUp(self):
    self.home = _fresh_home()
    from omegaforge.memory.wiki import Wiki
    self.w = Wiki()
    os.makedirs(self.w.dir, exist_ok=True)
    self.outside = os.path.join(self.home, "secret.md")
    with open(self.outside, "w", encoding="utf-8") as f:
      f.write("title: S\nupdated: 1\n\nTOPSECRET")

  def test_get_refuses_traversal(self):
    self.assertIsNone(self.w.get("../secret"), "越界读未被拦住")

  def test_delete_refuses_traversal(self):
    self.assertFalse(self.w.delete("../secret"))
    self.assertTrue(os.path.exists(self.outside), "越界删除未被拦住")

  def test_absolute_path_refused(self):
    self.assertIsNone(self.w.get("/etc/passwd"))

  def test_legitimate_slug_still_works(self):
    self.w.save("my-note", "我的笔记", "正文内容")
    page = self.w.get("my-note")
    self.assertIsNotNone(page)
    self.assertIn("正文内容", page["body"])
    self.assertTrue(self.w.delete("my-note"))

  def test_save_rejects_bad_slug_with_chinese_message(self):
    # 契约变更：ValueError → UserError。
    # 原因（）：这句中文明明写好了，但 ValueError 会被 _classify 一律
    # 压成「请求内容有误，请检查后重试」——**用户根本没看到它**。
    # 旧断言只看异常对象自带的 msg，等于验证了"代码里写了中文"，
    # 没验证"中文到达用户"，是个弱的无效的断言。
    # 这里同时验两件事：抛出 UserError，且经 user_error() 后文案原样保留。
    from omegaforge.core.errors import UserError, user_error
    with self.assertRaises(UserError) as ctx:
      self.w.save("Bad Slug!", "t", "b")
    msg = str(ctx.exception)
    # 报错文案面向用户：不得出现英文与 repr 形式的开发痕迹
    self.assertNotIn("invalid slug", msg)
    self.assertFalse(any(ord(c) < 128 and c.isalpha() for c in msg),
             f"报错文案含英文：{msg}")
    # 关键：必须穿过映射层原样到达用户，而不是被泛化文案吞掉
    self.assertEqual(user_error(ctx.exception), msg)
    self.assertNotIn("请检查后重试", user_error(ctx.exception))


class TestSkillNameSanitized(unittest.TestCase):
  """技能名未规整 → 可读/可删技能目录之外的路径。"""

  def setUp(self):
    _fresh_home()
    from omegaforge.skills.manager import SkillManager, _safe_name
    self.sm = SkillManager()
    self.safe = _safe_name

  def test_traversal_name_rejected(self):
    # 查找必须"认不出来就拒绝"，不能悄悄改写成另一个名字：
    # 否则 uninstall("../evil") 会去删名为 evil 的技能（目录内，但属误操作）
    for bad in ("../../..", "../evil", "/etc", "..", "", "..%2f..",
          "..\\evil", "a/b", ".", "..\u2215evil"):
      self.assertEqual(self.safe(bad), "", f"未拦住：{bad!r}")

  def test_normalize_never_escapes_root(self):
    # 安装侧的宽松规整：结果必须仍是合法目录名（即不可能越出技能根目录）
    from omegaforge.skills.manager import _normalize_name, _SAFE_NAME_RE
    for raw in ("My Skill", "../evil", "../../..", "/etc/passwd",
          "中文技能", " spaced ", "a/b/c"):
      out = _normalize_name(raw)
      self.assertTrue(out == "" or _SAFE_NAME_RE.match(out),
              f"规整结果可越界：{raw!r} -> {out!r}")
      self.assertNotIn("..", out)
      self.assertNotIn("/", out)

  def test_invoke_refuses_traversal(self):
    with self.assertRaises(FileNotFoundError):
      self.sm.invoke("../../../etc")

  def test_uninstall_refuses_traversal(self):
    self.assertFalse(self.sm.uninstall("../../.."))

  def test_legitimate_name_still_works(self):
    from omegaforge.skills.manager import _normalize_name
    # 安装侧规整："My Skill_1" -> "my-skill_1"（目录名必须小写化）
    self.assertEqual(_normalize_name("My Skill_1"), "my-skill_1")
    src = tempfile.mkdtemp()
    with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
      f.write("---\nname: My Skill_1\ndescription: d\nversion: 1\n---\nbody\n")
    info = self.sm.install(src)
    self.assertEqual(info["installed"], "my-skill_1")
    self.assertIn("body", self.sm.invoke("my-skill_1"))
    self.assertTrue(self.sm.uninstall("my-skill_1"))


if __name__ == "__main__":
  unittest.main(verbosity=2)
