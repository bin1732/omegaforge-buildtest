# -*- coding: utf-8 -*-
"""功能验收脚本自身的守卫。

## 为什么需要这个文件

"装完的应用能不能用"由 scripts/ci_verify_features.py 判定。它自己也可能
恒真——而它恒真时，验收报告里每一行都是 PASS，看不出任何功能其实没跑通。

手写清单与服务端路由脱节的两种形态：

  * /api/memory/remember 收的是 fact，验收清单里发的是 text → 400，
    而旧判定只拒绝 404 / 405 / 501，400 配一段结构化错误正文照样判 PASS；
  * /api/wiki/page 只认 GET 查询串，验收清单里发的是 POST → 404。

两者都说明：手写清单会与服务端路由脱节，而脱节在旧判定下不必然变红。

## 两条互补的口径

  1. 判定函数本身必须拒绝"被拒的请求"（4xx / 5xx 配结构化错误正文）；
  2. 清单里的每条（方法, 路径, 入参）必须真的被服务端接住。

第 1 条只对着判定函数，成本低但不能发现清单写错；第 2 条对着真实服务端，
清单写错必然变红。只有第 2 条的话，判定放宽（例如只要求"有响应"）时又
无从发现——两者缺一都会留下盲区。
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"


def load():
    spec = importlib.util.spec_from_file_location(
        "ci_verify_features", ROOT / "scripts" / "ci_verify_features.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verdict_of(*args):
    """跑一次判定，只要结论，不要它往诊断里写的行。"""
    m = load()
    return m.verdict([], *args)


class TestVerdictRejectsRejectedCalls:
    """请求被拒时不得判 PASS。

    入参契约漂移后，服务端回的是 4xx 加一段结构化错误正文——它是合法
    JSON、也不为空，三条旧口径（路由存在 / 是 JSON / 非空）全部满足，
    于是"功能不可用"被写成 PASS。
    """

    def test_400_with_structured_error_is_not_a_pass(self):
        body = json.dumps({"error": "请填写：要记住的内容",
                           "code": "E_INVALID_INPUT"})
        assert verdict_of("remember", "POST", "/api/memory/remember",
                          400, body) is False

    def test_500_with_structured_error_is_not_a_pass(self):
        body = json.dumps({"error": "内部错误", "code": "E_INTERNAL"})
        assert verdict_of("x", "POST", "/api/x", 500, body) is False

    def test_404_is_not_a_pass(self):
        assert verdict_of("x", "GET", "/api/x", 404,
                          json.dumps({"error": "接口不存在"})) is False

    def test_html_error_page_is_not_a_pass(self):
        assert verdict_of("x", "GET", "/api/x", 200,
                          "<html><body>error</body></html>") is False

    def test_empty_body_is_not_a_pass(self):
        assert verdict_of("x", "GET", "/api/x", 200, "") is False

    def test_empty_object_is_not_a_pass(self):
        assert verdict_of("x", "GET", "/api/x", 200, "{}") is False

    def test_real_payload_is_a_pass(self):
        body = json.dumps({"items": [{"id": "1"}]})
        assert verdict_of("tasks", "GET", "/api/tasks/list",
                          200, body) is True


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = str(tmp_path_factory.mktemp("accept_home"))
    os.environ["OMEGAFORGE_HOME"] = home
    os.environ["OMEGAFORGE_MODE"] = "auto_edit"
    import omegaforge.server as srv
    srv.RUNS.home = None
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield srv
    httpd.shutdown()


def _send(method: str, path: str, payload):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_declared_sequence_is_really_served(server):
    """清单里的每一步都得被服务端接住，且写入的内容要能读回来。

    按声明顺序执行而不是逐条独立跑：读回步骤依赖前面写入的数据（先存词条
    才能查详情、先建待办才能完成它），打乱顺序会把"顺序依赖"误报成功能坏了。

    失败一次性全部列出：只报第一条的话，后面还有几条失效是看不见的，容易
    被当成单点问题修掉。
    """
    m = load()
    ctx: dict = {}
    bad = []
    for name, method, path, payload, expect in m.STEPS:
        status, body = _send(method, path, m.resolve(payload, ctx))
        if status >= 400:
            bad.append(f"{method} {name} 被拒（{status}）：{body[:120]}")
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            bad.append(f"{method} {name} 响应不是 JSON：{body[:120]}")
            continue
        if parsed in (None, {}, "", []):
            bad.append(f"{method} {name} 响应为空壳：{body[:120]}")
            continue
        if expect is not None and expect not in body:
            bad.append(f"{method} {name} 写入的内容读不回来：{expect!r} "
                       f"不在 {body[:120]!r} 里")
        if isinstance(parsed, dict):
            ctx[name] = parsed
    assert not bad, "验收清单与真实服务端不一致：\n  " + "\n  ".join(bad)


def test_steps_cover_both_directions():
    """清单里必须有"写完读回"的步骤。

    只有单向的写请求时，"返回 200 但没落盘"不会被任何一步发现——200 且
    非空，全部口径都满足。因此至少要有一步的期望串来自前面的写入。
    """
    m = load()
    read_back = [s for s in m.STEPS if s[4] is not None]
    assert read_back, "没有任何一步校验写入的内容能读回来"
    refs = [s for s in m.STEPS
            if isinstance(s[3], dict)
            and any(isinstance(v, str) and v.startswith("$")
                    for v in s[3].values())]
    assert refs, "没有任何一步引用前面返回的标识：完成类操作手写常量会恒失败"
