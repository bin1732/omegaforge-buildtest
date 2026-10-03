# -*- coding: utf-8 -*-
"""server 侧跨版本比对 + /api/runs 列表守卫。

为什么必须起真实 HTTP 服务来测
------------------------------
直接调 `compare_reports` 只能验算法，验不出**路由是否接上**。
本项目已多次栽在"只测内层、接通断了也全绿"上，所以这里一律走 HTTP。

本文件同时守住 /api/runs 的性能契约：原实现对每个 run 调 get()
→ 回放整份事件流来算 seq，**再**截断到 limit，于是列表耗时随历史
总量线性增长（验证 330 个 run：3.0 秒），而用户看到的永远只有前 50 条。
"""
import json
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest # noqa: E402
from http.server import ThreadingHTTPServer # noqa: E402

import omegaforge.server as S # noqa: E402
from omegaforge.core.run import RunStore # noqa: E402
from omegaforge.distill.engine import DistillEngine # noqa: E402

_CASES = [{"id": "c1", "input": "把这段文本按句切分", "rubric": ["正确性"]}]
_FP = DistillEngine.eval_set_fingerprint(_CASES)
_SRC = ("You are a helpful assistant that writes clean Python code. "
    "Always explain your reasoning step by step and return only "
    "the requested output.")


def _mk_run(root: str, rid: str, *, score, fingerprint, claim=True,
      comparable=True, with_report=True):
  d = os.path.join(root, rid)
  os.makedirs(d, exist_ok=True)
  meta = {"id": rid, "status": "succeeded", "phase": "succeeded",
      "created_ts": time.time(), "final_score": score,
      "eval_set_fingerprint": fingerprint, "eval_set_cases": 1,
      "claim_valid": claim, "baseline_comparable": comparable}
  pathlib.Path(os.path.join(d, "run.json")).write_text(
    json.dumps(meta, ensure_ascii=False), encoding="utf-8")
  pathlib.Path(os.path.join(d, "events.jsonl")).write_text(
    json.dumps({"seq": 3, "type": "done", "message": "x"}) + "\n",
    encoding="utf-8")
  if with_report:
    pathlib.Path(os.path.join(d, "report.json")).write_text(
      json.dumps({"final_score": score,
            "eval_set_fingerprint": fingerprint,
            "baseline_comparable": comparable,
            "claim_valid": claim}, ensure_ascii=False),
      encoding="utf-8")
  return d


@pytest.fixture()
def srv(tmp_path, monkeypatch):
  """起真实服务，并把 RUNS 指向临时目录（不污染真实历史）。"""
  runs = os.path.join(str(tmp_path), "runs")
  os.makedirs(runs, exist_ok=True)
  monkeypatch.setattr(S, "RUNS", RunStore(home=str(tmp_path)), raising=True)
  httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  base = f"http://127.0.0.1:{httpd.server_address[1]}"
  yield base, runs
  httpd.shutdown()


def _get(base, path):
  import urllib.request
  import urllib.error
  try:
    with urllib.request.urlopen(base + path, timeout=30) as r:
      return r.status, json.load(r)
  except urllib.error.HTTPError as e:
    return e.code, json.load(e)


# -- /api/compare ----------------------------------------------------------

def test_compare_gives_delta_on_same_eval_set(srv):
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  _mk_run(runs, "bbbbbbbbbbbb", score=8.4, fingerprint=_FP)
  code, body = _get(base, "/api/compare?a=aaaaaaaaaaaa&b=bbbbbbbbbbbb")
  assert code == 200
  assert body["comparable"] is True
  assert body["delta"] == 3.4


def test_compare_refuses_different_eval_set(srv):
  """题不同必须拒绝给 delta——给一个精确数字是最坏结果。"""
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  _mk_run(runs, "bbbbbbbbbbbb", score=8.4, fingerprint="deadbeefdeadbeef")
  code, body = _get(base, "/api/compare?a=aaaaaaaaaaaa&b=bbbbbbbbbbbb")
  assert code == 200
  assert body["comparable"] is False and body["delta"] is None
  assert "不可比" in body["reason"]


def test_compare_refuses_when_claim_invalid(srv):
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP, claim=False)
  _mk_run(runs, "bbbbbbbbbbbb", score=8.4, fingerprint=_FP)
  _code, body = _get(base, "/api/compare?a=aaaaaaaaaaaa&b=bbbbbbbbbbbb")
  assert body["comparable"] is False
  assert "结论本身不成立" in body["reason"]


def test_compare_requires_two_ids(srv):
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  code, body = _get(base, "/api/compare?a=aaaaaaaaaaaa")
  assert code == 400
  assert "两个任务编号" in body.get("error", "")


def test_compare_unknown_run_is_404(srv):
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  code, _b = _get(base, "/api/compare?a=aaaaaaaaaaaa&b=cccccccccccc")
  assert code == 404


def test_compare_when_report_not_ready(srv):
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  _mk_run(runs, "bbbbbbbbbbbb", score=8.4, fingerprint=_FP,
      with_report=False)
  code, body = _get(base, "/api/compare?a=aaaaaaaaaaaa&b=bbbbbbbbbbbb")
  assert code == 404
  assert "尚未生成" in body.get("error", "")


def test_compare_rejects_path_traversal(srv):
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  code, _b = _get(base, "/api/compare?a=aaaaaaaaaaaa&b=..%2F..%2Fetc")
  assert code == 404


# -- /api/runs -------------------------------------------------------------

def test_runs_exposes_fingerprint_in_meta(srv):
  """列表里必须能看到指纹，否则界面无从判断哪两次可比。"""
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  _code, body = _get(base, "/api/runs")
  row = [r for r in body["runs"] if r["id"] == "aaaaaaaaaaaa"][0]
  assert row["meta"]["eval_set_fingerprint"] == _FP


def test_runs_seq_is_not_zero(srv):
  """seq 恒 0 会让前端每次从头重拉整份日志。

  历史 run（seq 缓存引入之前写的）没有该字段，必须回退读事件流补上，
  否则列表里 seq 全是 0——这是"加缓存"最容易引入的静默回归。
  """
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  _code, body = _get(base, "/api/runs")
  row = [r for r in body["runs"] if r["id"] == "aaaaaaaaaaaa"][0]
  assert row["seq"] == 3, f"历史 run 的 seq 应回退补出，实际 {row['seq']}"


def test_real_distill_persists_fingerprint(srv):
  """走真实 distill，验证指纹真的写进了 run.json。

  为什么必须真跑：上面几个用例的 run.json 都是**自己造的**，
  撤掉 server 里写指纹的代码它们照样全绿（验证抓不到）。
  "自己造输入、自己验输出"是最容易出现的假绿。
  """
  import urllib.request
  base, runs = srv
  payload = {"source_prompt": _SRC, "budget": 300000, "rounds": 1,
        "gens": 1, "eval_set": _CASES}
  req = urllib.request.Request(
    base + "/api/distill", data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"})
  job = json.load(urllib.request.urlopen(req, timeout=30))["job"]
  for _ in range(90):
    time.sleep(1)
    _c, st = _get(base, f"/api/jobs/{job}")
    if st.get("status") in ("done", "error"):
      break
  assert st.get("status") == "done", f"distill 未完成：{st}"
  _c, body = _get(base, "/api/runs")
  row = [r for r in body["runs"] if r["id"] == job]
  assert row, "任务未出现在列表中"
  assert row[0]["meta"].get("eval_set_fingerprint") == _FP
  assert row[0]["meta"].get("claim_valid") is not None


def test_runs_sorted_newest_first(srv):
  base, runs = srv
  for i, rid in enumerate(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]):
    _mk_run(runs, rid, score=5.0, fingerprint=_FP)
    time.sleep(0.01)
  _code, body = _get(base, "/api/runs")
  ts = [r.get("created_ts") or 0 for r in body["runs"]]
  assert ts == sorted(ts, reverse=True)


def test_runs_does_not_read_event_streams(srv, monkeypatch):
  """列表不该逐份回放事件流——那是耗时随历史线性增长的根源。

  只在**字段缺失**时回退，带缓存的新 run 绝不读事件流。
  """
  base, runs = srv
  _mk_run(runs, "aaaaaaaaaaaa", score=5.0, fingerprint=_FP)
  # 给个带 seq 缓存的 meta，再把事件流替换成会炸的内容：
  # 若实现仍去回放，这里必然抛错或返回异常。
  mp = os.path.join(runs, "aaaaaaaaaaaa", "run.json")
  m = json.loads(pathlib.Path(mp).read_text(encoding="utf-8"))
  m["seq"] = 7
  pathlib.Path(mp).write_text(json.dumps(m), encoding="utf-8")
  pathlib.Path(os.path.join(runs, "aaaaaaaaaaaa", "events.jsonl")).write_text(
    "\x00\x00not json at all\n", encoding="utf-8")
  _code, body = _get(base, "/api/runs")
  row = [r for r in body["runs"] if r["id"] == "aaaaaaaaaaaa"][0]
  assert row["seq"] == 7


if __name__ == "__main__":
  sys.exit(pytest.main([__file__, "-q"]))
