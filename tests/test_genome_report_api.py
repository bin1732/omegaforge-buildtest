# -*- coding: utf-8 -*-
"""/api/genome/<job> 与 /api/report/<job> 端到端回归测试。

背景：这两个端点缺少该约束时只存在于 server.py 头部的文档注释中，没有实现体
（"文档声称、代码缺失"的僵尸路由）。补齐实现后，用真实 HTTP server
跑通蒸馏并取回产物，防止再次退化。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from omegaforge import server # noqa: E402


@pytest.fixture(scope="module")
def live_server():
  srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
  port = srv.server_address[1]
  t = threading.Thread(target=srv.serve_forever, daemon=True)
  t.start()
  time.sleep(0.4)
  yield f"http://127.0.0.1:{port}"
  srv.shutdown()


def _get(base: str, path: str):
  with urllib.request.urlopen(base + path, timeout=60) as r:
    return json.load(r)


def _post(base: str, path: str, payload: dict):
  req = urllib.request.Request(
    base + path,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
  )
  with urllib.request.urlopen(req, timeout=180) as r:
    return json.load(r)


def _finished_job(base: str) -> str:
  """跑一次蒸馏并等到终态，返回 job id。"""
  r = _post(base, "/api/distill", {"source": "You are a rigorous analyst. Cite sources.", "name": "t"})
  # 真实契约是 job / run，不是 job_id（曾因此取到 undefined 导致 404）
  jid = r.get("job")
  assert jid, f"distill 未返回 job id: {r}"
  for _ in range(240):
    st = _get(base, f"/api/jobs/{jid}")
    if st.get("status") in ("done", "error", "cancelled"):
      break
    time.sleep(0.5)
  assert st.get("status") == "done", f"蒸馏未正常完成: {st.get('status')}"
  return jid


def test_genome_endpoint_returns_artifact(live_server):
  jid = _finished_job(live_server)
  g = _get(live_server, f"/api/genome/{jid}")
  assert isinstance(g, dict) and g, "genome 为空"
  # 基因组必须携带身份与谱系字段，否则下游无法做进化对比
  for key in ("id", "schema_version", "persona_genes"):
    assert key in g, f"genome 缺少关键字段 {key}: {sorted(g)[:12]}"


def test_report_endpoint_returns_artifact(live_server):
  jid = _finished_job(live_server)
  rp = _get(live_server, f"/api/report/{jid}")
  assert isinstance(rp, dict) and rp, "report 为空"
  # 报告必须含裁决与诚实基线元信息
  for key in ("verdict", "final_score", "baseline_kind", "baseline_comparable"):
    assert key in rp, f"report 缺少关键字段 {key}: {sorted(rp)[:12]}"


def test_unknown_job_returns_404(live_server):
  for path in ("/api/genome/nope", "/api/report/nope"):
    with pytest.raises(urllib.error.HTTPError) as ei:
      _get(live_server, path)
    assert ei.value.code == 404, f"{path} 应 404，实际 {ei.value.code}"
