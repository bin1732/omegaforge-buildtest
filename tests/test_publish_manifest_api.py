# -*- coding: utf-8 -*-
"""产物清单回传（Git Data API）的用例：对本地假 API 服务端端到端跑。

每条断言对应一种具体失效：
  · 分支不存在却仍带父提交 → 提交会 404，产物分支永远建不起来
  · 分支存在却建孤儿提交   → 远端历史被覆盖，历史清单丢失，无法对账
  · 空清单/非法 JSON 静默绿 → 分支上留下一个空文件，看着"回传成功"
    而这一轮的包多大、校验和是多少全都无从复查
  · 不回读校验             → POST 成功但分支上没这个文件，与没写完全一样
  · 工作区脏就失败         → 这正是 git checkout 版本的真实故障：
    前端构建改了 package-lock.json，checkout 直接中止，报的是 git 的错
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_manifest_api.py"

REMOTE_SHA = "a" * 40
BLOB_SHA = "b" * 40


class _Handler(BaseHTTPRequestHandler):
    branch_exists = True
    readback_sha = BLOB_SHA
    calls: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

    def do_GET(self):
        type(self).calls.append("GET " + self.path)
        if "/git/ref/heads/" in self.path:
            if not type(self).branch_exists:
                self._send({"message": "Not Found"}, 404)
                return
            self._send({"object": {"sha": REMOTE_SHA}})
            return
        if "/contents/" in self.path:
            self._send({"sha": type(self).readback_sha})
            return
        self._send({}, 404)

    def do_POST(self):
        payload = self._read()
        type(self).calls.append("POST " + self.path)
        if self.path.endswith("/git/blobs"):
            self._send({"sha": BLOB_SHA})
        elif self.path.endswith("/git/trees"):
            type(self).tree_payload = payload
            self._send({"sha": "c" * 40})
        elif self.path.endswith("/git/commits"):
            type(self).commit_payload = payload
            self._send({"sha": "d" * 40})
        elif self.path.endswith("/git/refs"):
            type(self).ref_payload = payload
            self._send({})
        else:
            self._send({}, 404)

    def do_PATCH(self):
        self._read()
        type(self).calls.append("PATCH " + self.path)
        self._send({})


def _serve(branch_exists=True, readback_sha=BLOB_SHA):
    cls = type("H", (_Handler,), {"branch_exists": branch_exists,
                                  "readback_sha": readback_sha,
                                  "calls": [], "tree_payload": None,
                                  "commit_payload": None, "ref_payload": None})
    srv = HTTPServer(("127.0.0.1", 0), cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, cls


def _run(manifest: Path, env_extra=None, branch="artifacts"):
    # 令牌变量都先从基线里清掉再合并：只清 GH_API_BASE 的话，外部环境里的
    # 令牌会以别的名字漏进来（脚本认 OF_PAT / GITHUB_TOKEN / OF_GITHUB_TOKEN
    # / GH 四个），"缺令牌必须失败"这条在外层有令牌时永远测不到——而它测的
    # 恰恰是"无令牌静默绿"：发布步骤显示成功而分支上什么都没有。
    env = dict(os.environ)
    env.pop("GH_API_BASE", None)
    for k in ("OF_PAT", "GITHUB_TOKEN", "OF_GITHUB_TOKEN", "GH"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "o/r", "--branch", branch,
         "--file", str(manifest), "--path", "installer/manifest.json"],
        capture_output=True, text=True, cwd=str(ROOT), env=env)


def _manifest(d: str, data=None) -> Path:
    p = Path(d) / "manifest.json"
    p.write_text(json.dumps(data if data is not None
                            else {"bytes": 1, "sha256": "x"}),
                 encoding="utf-8")
    return p


def test_existing_branch_commits_with_parent_and_patches_ref():
    with tempfile.TemporaryDirectory() as d:
        srv, cls = _serve()
        try:
            r = _run(_manifest(d), {"GH": "t", "GH_API_BASE":
                                    f"http://127.0.0.1:{srv.server_port}"})
            assert r.returncode == 0, r.stdout + r.stderr
            assert cls.commit_payload["parents"] == [REMOTE_SHA]
            assert cls.tree_payload["base_tree"] == REMOTE_SHA
            assert any(c.startswith("PATCH") for c in cls.calls)
            assert "回读一致" in r.stdout
        finally:
            srv.shutdown()


def test_missing_branch_creates_orphan():
    with tempfile.TemporaryDirectory() as d:
        srv, cls = _serve(branch_exists=False)
        try:
            r = _run(_manifest(d), {"GH": "t", "GH_API_BASE":
                                    f"http://127.0.0.1:{srv.server_port}"})
            assert r.returncode == 0, r.stdout + r.stderr
            assert cls.commit_payload["parents"] == []
            assert cls.ref_payload["ref"] == "refs/heads/artifacts"
        finally:
            srv.shutdown()


def test_readback_mismatch_fails():
    with tempfile.TemporaryDirectory() as d:
        srv, _ = _serve(readback_sha="e" * 40)
        try:
            r = _run(_manifest(d), {"GH": "t", "GH_API_BASE":
                                    f"http://127.0.0.1:{srv.server_port}"})
            assert r.returncode == 1, r.stdout
            assert "回读不一致" in r.stdout
        finally:
            srv.shutdown()


def test_empty_manifest_fails_not_silent_green():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.json"
        p.write_text("   \n", encoding="utf-8")
        srv, _ = _serve()
        try:
            r = _run(p, {"GH": "t", "GH_API_BASE":
                         f"http://127.0.0.1:{srv.server_port}"})
            assert r.returncode == 1, r.stdout
            assert "为空" in r.stdout
        finally:
            srv.shutdown()


def test_invalid_json_fails():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.json"
        p.write_text("{not json", encoding="utf-8")
        srv, _ = _serve()
        try:
            r = _run(p, {"GH": "t", "GH_API_BASE":
                         f"http://127.0.0.1:{srv.server_port}"})
            assert r.returncode == 1, r.stdout
            assert "合法 JSON" in r.stdout
        finally:
            srv.shutdown()


def test_missing_token_fails():
    with tempfile.TemporaryDirectory() as d:
        srv, _ = _serve()
        try:
            env = dict(os.environ)
            env.pop("GH", None)
            env["GH_API_BASE"] = f"http://127.0.0.1:{srv.server_port}"
            r = _run(_manifest(d), env)
            assert r.returncode == 1, r.stdout
            assert "令牌" in r.stdout
        finally:
            srv.shutdown()


def test_dirty_worktree_does_not_block_publish():
    """工作区脏不影响回传——这正是 git checkout 版本的真实故障。"""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        tracked = Path(d) / "frontend" / "package-lock.json"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text('{"a": 1}', encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a",
                        "commit", "-qm", "init"], cwd=d, check=True)
        tracked.write_text('{"a": 2}', encoding="utf-8")  # 制造脏工作区
        srv, _ = _serve()
        try:
            r = _run(_manifest(d), {"GH": "t", "GH_API_BASE":
                                    f"http://127.0.0.1:{srv.server_port}"})
            assert r.returncode == 0, r.stdout + r.stderr
        finally:
            srv.shutdown()
