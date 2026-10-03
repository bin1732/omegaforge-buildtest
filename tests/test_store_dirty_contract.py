"""会话存储 / 用量账本的脏数据契约守卫。

背景（全部为可复现，不是理论推演）：

  list() 遇脏会话   -> TypeError: object of type 'NoneType' has no len()
  summary() 遇脏账本  -> TypeError: unsupported operand type(s) for +=:
              'int' and 'str'
  new(title=None)   -> TypeError: 'NoneType' object is not subscriptable
  POST /api/conversations/new {"title": null} -> 标题被存成字面量 "None"

共同特征：**一行脏数据 → 整个页面永久打不开，且没有任何清理入口**。
用户看到的是"操作失败，请稍后重试"，重试一百次也一样，因为他不知道
是自己哪条历史记录坏了。这类问题不会自己消失，只能手工改文件。

因此本文件的原则：单条脏记录跳过/归正，绝不连累全局。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
  (PASS if cond else FAIL).append(name)
  print(f" [{'PASS' if cond else 'FAIL'}] {name}"
     + (f" — {detail}" if detail else ""))


def check_http() -> None:
  """真实起服务，验证脏数据下页面唯一读路径仍可用。"""
  import threading
  import time
  import urllib.error
  import urllib.request
  from http.server import ThreadingHTTPServer

  home = tempfile.mkdtemp(prefix="of_http_")
  os.makedirs(os.path.join(home, "conversations"), exist_ok=True)
  with open(os.path.join(home, "conversations", "a" * 10 + ".json"),
       "w", encoding="utf-8") as f:
    json.dump({"id": "a" * 10, "messages": None}, f)
  with open(os.path.join(home, "usage.jsonl"), "w", encoding="utf-8") as f:
    f.write('{"ts":1,"phase":"p","model":"m","tokens":"127"}\n')
    f.write('{"ts":"x","phase":"p","model":"m","tokens":-50}\n')

  # 快照并还原：本套件若被 pytest 与其他测试同批执行，改掉 OMEGAFORGE_HOME
  # 又不还原，会让后续测试静默跑在错误的目录上（缺少该约束时踩过同类隔离缺陷）。
  prev_env = os.environ.get("OMEGAFORGE_HOME")
  import omegaforge.server as srv
  prev_home = srv.RUNS.home
  # 会话目录同样要指向本次 home：CONVS 是模块级单例，只改环境变量不会重指向。
  # 漏了这一句，6.2/6.3 会写进上一个套件（已被删除）的临时目录而报
  # FileNotFoundError——产品行为其实是对的，是测试没接通。
  prev_convs_home = srv.CONVS.home
  os.environ["OMEGAFORGE_HOME"] = home
  # runs_dir / CONVS.dir 已是只读 property，从 home 现算，无需手动同步
  srv.RUNS.home = home
  srv.CONVS.home = home
  os.makedirs(srv.CONVS.dir, exist_ok=True)
  port = 8977
  try:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
  except OSError as e:
    check("6.0 起服务", False, str(e))
    return
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  time.sleep(0.4)
  base = f"http://127.0.0.1:{port}"

  def get(path):
    try:
      with urllib.request.urlopen(base + path, timeout=15) as r:
        return r.status
    except urllib.error.HTTPError as e:
      return e.code

  def post(path, body):
    req = urllib.request.Request(
      base + path, data=json.dumps(body).encode(),
      headers={"Content-Type": "application/json"})
    try:
      with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
      return e.code, {}

  try:
    for path in ("/api/status", "/api/usage", "/api/conversations"):
      code = get(path)
      check(f"6.1 {path} 不为 500", code == 200, f"HTTP {code}")
    code, body = post("/api/conversations/new", {"title": None})
    check("6.2 title=null 不留字面量 None",
       code == 200 and body.get("title") == "新对话",
       f"HTTP {code} title={body.get('title')!r}")
    code, body = post("/api/conversations/new", {"title": []})
    check("6.3 title=[] 不留字面量 []",
       code == 200 and body.get("title") == "新对话",
       f"HTTP {code} title={body.get('title')!r}")
  finally:
    httpd.shutdown()
    httpd.server_close()
    srv.RUNS.home = prev_home
    srv.CONVS.home = prev_convs_home
    if prev_env is None:
      os.environ.pop("OMEGAFORGE_HOME", None)
    else:
      os.environ["OMEGAFORGE_HOME"] = prev_env


def main() -> int:
  from omegaforge.chat.store import ConversationStore
  from omegaforge.llm.usage import UsageStore

  # ── 1. 用量账本：脏记录不连累全局 ──────────────────────────────
  home = tempfile.mkdtemp(prefix="of_usage_")
  u = UsageStore(home=home)
  dirty = [
    '{"ts":1,"phase":"p","model":"m","tokens":"127"}',  # 字符串 token
    '{"ts":1,"phase":"p","model":"m","tokens":null}',  # null
    '{"ts":1,"phase":"p","model":"m","tokens":-50}',   # 负数（倒扣）
    '{"ts":"abc","phase":"p","model":"m","tokens":5}',  # ts 非数字
    '{"ts":9e18,"phase":"p","model":"m","tokens":5}',  # ts 越界
    '{"phase":"p","model":"m","tokens":5}',       # 缺 ts
    '[1,2,3]',                      # 整行不是对象
    'not json at all',                  # 坏行
  ]
  with open(u.path, "w", encoding="utf-8") as f:
    f.write("\n".join(dirty) + "\n")
  try:
    s = u.summary()
    ok = True
    detail = ""
  except Exception as e:                 # noqa: BLE001
    ok, s, detail = False, {}, f"{type(e).__name__}: {e}"
  check("1.1 脏账本不崩溃", ok, detail)
  # 127(字符串归正) + 5×3 = 142；null→0，负数→0。
  # 三条 ts 不可识别/缺失的记录仍计入总量，只是不进时间线。
  check("1.2 字符串 token 归正、负数不倒扣",
     ok and s.get("total") == 142, f"total={s.get('total')}")
  check("1.3 总条数只数有效行", ok and s.get("entries") == 6,
     f"entries={s.get('entries')}")
  check("1.4 时间线不越界",
     ok and len(s.get("timeline", [])) == 24
     and all(isinstance(x, int) for x in s["timeline"]))

  # 写入侧夹紧
  h2 = tempfile.mkdtemp(prefix="of_usage2_")
  u2 = UsageStore(home=h2)
  u2.record("chat", "m", -100, -5, -7)
  u2.record("chat", "m", "300", "12", "7")   # 上游返回字符串
  s2 = u2.summary()
  check("1.5 负数用量不入账", s2["total"] == 300, f"total={s2['total']}")
  check("1.6 字符串用量归正为整数", s2["total"] == 300,
     "字符串 '300' 按 300 计，不做字符串拼接")
  # 写入侧必须自己干净：读盘时归正只能救本进程，磁盘上的负数会污染
  # 任何别的读取方（更早版本、手工排查、导出）。所以直接查原始行。
  with open(u2.path, encoding="utf-8") as f:
    rows = [json.loads(l) for l in f if l.strip()]
  check("1.7 落盘不出现负数（写入侧夹紧，而非靠读取侧补救）",
     all(r["tokens"] >= 0 and r["prompt"] >= 0 and r["completion"] >= 0
       for r in rows),
     str([r["tokens"] for r in rows]))
  check("1.8 落盘为整数（字符串 token 不原样入库）",
     all(isinstance(r["tokens"], int) for r in rows),
     str([(r["tokens"], type(r["tokens"]).__name__) for r in rows]))

  # ── 2. 会话存储：脏会话不连累列表 ─────────────────────────────
  chome = tempfile.mkdtemp(prefix="of_conv_")
  c = ConversationStore(home=chome)
  convs = [
    {"id": "a" * 10},                  # 缺绝大多数字段
    {"id": "b" * 10, "title": "T", "model_pref": "auto",
     "updated": 1, "messages": None},          # messages=null
    {"id": "c" * 10, "title": "T", "model_pref": "auto",
     "updated": 1, "messages": [{"role": "user"}]},   # 消息缺 content
    {"id": "d" * 10, "title": "T", "model_pref": "auto",
     "updated": 1, "messages": [{"role": "user", "content": 123}]},
    {"id": "e" * 10, "title": "T", "model_pref": "auto",
     "updated": "abc", "messages": []},         # updated 非数字
    {"id": "f" * 10, "title": "T", "model_pref": "auto",
     "updated": 2, "messages": [], "summary": {"x": 1}}, # summary 非串
  ]
  for cv in convs:
    with open(os.path.join(c.dir, cv["id"] + ".json"), "w",
         encoding="utf-8") as f:
      json.dump(cv, f)
  try:
    lst = c.list()
    ok = True
    detail = ""
  except Exception as e:                 # noqa: BLE001
    ok, lst, detail = False, [], f"{type(e).__name__}: {e}"
  check("2.1 列表遇脏会话不崩溃", ok, detail)
  check("2.2 脏会话仍全部列出（跳过而非静默丢失）",
     ok and len(lst) == 6, f"len={len(lst) if ok else '-'}")
  check("2.3 缺字段会话有可读标题",
     ok and all(x["title"] for x in lst),
     ",".join(x["title"] for x in lst) if ok else "-")
  check("2.4 count 为整数（messages=null 按 0 计）",
     ok and all(isinstance(x["count"], int) for x in lst))
  check("2.5 updated 可排序（非数字排最后而非崩溃）",
     ok and all(isinstance(x["updated"], float) for x in lst))

  # ── 3. 新建会话：空标题不留 "None" ─────────────────────────────
  t1 = c.new(None)["title"]
  check("3.1 title=None -> 默认标题", t1 == "新对话", f"title={t1!r}")
  t2 = c.new("")["title"]
  check("3.2 title='' -> 默认标题", t2 == "新对话", f"title={t2!r}")
  t3 = c.new(123)["title"]
  check("3.3 title=123 不崩溃", t3 == "123", f"title={t3!r}")
  t4 = c.new("我的会话")["title"]
  check("3.4 正常标题原样保留", t4 == "我的会话", f"title={t4!r}")

  # ── 4. 追加消息：脏历史不吞掉新消息 ───────────────────────────
  for cid in ("a" * 10, "b" * 10, "c" * 10, "d" * 10):
    try:
      c.add_message(cid, "user", "新消息")
      ok = True
      detail = ""
    except Exception as e:               # noqa: BLE001
      ok, detail = False, f"{type(e).__name__}: {e}"
    check(f"4.x add_message 到脏会话 {cid[0]}", ok, detail)

  # 写入的正文本身也可能是脏的（content=123 会让整条消息丢失）
  for bad in (None, {"a": 1}, 123, ["x"], True):
    cid = c.new("脏内容用例")["id"]
    try:
      got = c.add_message(cid, "user", bad)["messages"][-1]["content"]
      ok = isinstance(got, str)
      detail = f"{bad!r} -> {got!r}"
    except Exception as e:               # noqa: BLE001
      ok, detail = False, f"{type(e).__name__}: {e}"
    check(f"4.y add_message 脏正文 {type(bad).__name__}", ok, detail)

  # ── 5. 压缩与上下文构建：脏内容不崩 ───────────────────────────
  conv = {"id": "0" * 10,
      "messages": [{"role": "user", "content": None},
             {"role": "user", "content": {"a": 1}},
             {"role": "assistant"}] + [
        {"role": "user", "content": "长文本" * 3000} for _ in range(6)],
      "summary": {"x": 1}}
  try:
    from omegaforge.chat.store import ConversationStore as CS
    CS(home=chome)._maybe_compact(conv)
    ok = True
    detail = ""
  except Exception as e:                 # noqa: BLE001
    ok, detail = False, f"{type(e).__name__}: {e}"
  check("5.1 压缩遇脏消息不崩溃", ok, detail)
  try:
    sysm, task = CS(home=chome).build_context(conv, "任务")
    ok = isinstance(sysm, str) and isinstance(task, str)
  except Exception as e:                 # noqa: BLE001
    ok, detail = False, f"{type(e).__name__}: {e}"
  check("5.2 build_context 遇脏 summary 不崩溃", ok, detail)

  # ── 6. 接口层：脏数据不得让页面端点变成 500 ───────────────────
  # 只在接口层才测得到：store 层收不到"调用方先把 None str() 成 'None'"
  # 这种错误，而用户看到的就是列表里一个叫 None 的对话。
  check_http()

  print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
  if FAIL:
    print("失败项：" + "; ".join(FAIL))
  return 1 if FAIL else 0


def test_store_dirty_contract() -> None:
  """pytest 入口：让本套件在 CI 里也真跑起来。

  不加这一层，本文件在 pytest 下收集到 0 个用例——守卫写了但永远不执行，
  正是"反复修、反复漏"的结构性原因之一。
  """
  assert main() == 0


if __name__ == "__main__":
  raise SystemExit(main())
