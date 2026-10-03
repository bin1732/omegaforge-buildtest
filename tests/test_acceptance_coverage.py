# -*- coding: utf-8 -*-
"""装机功能验收的覆盖面守卫：没被请求过的端点必须说出来。

## 守的是什么

验收报告写的是"功能验收 N/N 通过"。这句话只说明**被请求过的**那些通过了；
未被任何步骤碰到的端点不会让报告变红，于是"通过"会被读成"每个功能都验过"。
路由改删、字段名漂移、提示文案被换掉，这三类失效全落在没被请求的那部分里。

## 三层口径

    功能级  真的写入并读回，证明功能生效
    契约级  空入参打过去，端点须给出自己那条提示（证明路由与契约还在）
    未探测  两者都不是，必须登记理由，并在报告里单独报出

契约级为什么能成立：这些端点在验收环境里跑不完整流程（要真实模型、
音频、长流水线或已完成的运行），但它们的入参校验**早于**任何副作用——
空入参必然在校验处返回，不会真的发起调用。故探测是安全的。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "ci_verify_features", os.path.join(ROOT, "scripts", "ci_verify_features.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


M = _load()


class CoverageTest(unittest.TestCase):
    def test_routes_readable(self):
        routes, _ = M.coverage()
        self.assertTrue(routes, "取不到后端路由 —— 取不到不能判全覆盖")

    def test_every_route_is_probed(self):
        _, missing = M.coverage()
        self.assertEqual([], missing, f"未探测且未登记：{missing}")

    def test_unprobed_registry_has_reasons(self):
        for route, why in M.UNPROBED.items():
            self.assertTrue(why.strip(), f"{route} 登记了未探测但理由为空")

    def test_reach_steps_well_formed(self):
        """期望正文必须非空——空串会让 `want_text in body` 恒真，
        契约级步骤会全部通过而什么都没验到。"""
        for name, method, path, _payload, status, text in M.REACH_STEPS:
            self.assertTrue(text.strip(), f"{name} 的期望正文为空")
            self.assertIn(method, ("GET", "POST"), f"{name} 方法非法")
            self.assertIn(status, (400, 404, 409), f"{name} 期望状态码异常")
            self.assertTrue(path.startswith("/api/"), f"{name} 路径非法")

    def test_no_reach_step_in_functional_steps(self):
        """同一端点不得重复登记：重复会让覆盖面看着更满，实际少验。"""
        funcs = {p.split("?")[0].rstrip("/")
                 for _n, _m, p, *_ in M.STEPS}
        for _n, _m, p, *_ in M.REACH_STEPS:
            self.assertNotIn(p.rstrip("/"), funcs, f"{p} 同时登记在两级里")


class ReverseTest(unittest.TestCase):
    """判定本身要能被反例证伪。"""

    def test_dropping_a_reach_step_is_reported(self):
        orig = list(M.REACH_STEPS)
        try:
            M.REACH_STEPS = [s for s in orig if s[0] != "compare"]
            _, missing = M.coverage()
            self.assertIn("/api/compare", missing, missing)
        finally:
            M.REACH_STEPS = orig

    def test_empty_expectation_is_rejected(self):
        """期望正文被清空时判定必须失败，否则契约级会退化成恒真。"""
        self.assertFalse(M.reach_verdict(
            [], "x", "/api/x", 400, '{"error":"任意"}', 400, ""))

    def test_missing_route_diagnosed_distinctly(self):
        """路由被删时同样回 404；若只比状态码，
        '接口不存在' 会被当成'任务不存在'判通过。"""
        self.assertFalse(M.reach_verdict(
            [], "x", "/api/x", 404, '{"error":"接口不存在"}', 404, "未找到该任务"))


class LiveContractTest(unittest.TestCase):
    """契约级清单必须对真后端成立——否则清单是与实现脱节的臆测。"""

    @classmethod
    def setUpClass(cls):
        from omegaforge.server import Handler
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_reach_steps_against_real_server(self):
        bad = []
        for name, method, path, payload, want_status, want_text in M.REACH_STEPS:
            st, body = M.call(method, self.port, path, payload)
            if ("接口不存在" in body or st != want_status
                    or want_text not in body):
                bad.append(f"{name}: {st} (期望 {want_status}) {body[:100]!r}")
        self.assertEqual([], bad, "\n".join(bad))


if __name__ == "__main__":
    unittest.main()


class PipelineVerdictTest(unittest.TestCase):
    """流水线自身的判定要能被反例证伪。

    这些用例用桩替换真实请求，验证"产物不对时必须报失败"——
    若判定恒真，装机报告上的 13/13 就说明不了任何事。
    """

    def _run(self, responses):
        orig = M.call
        M.call = lambda method, port, path, payload: responses.get(
            (method, path.split("?")[0]), (404, "{}"))
        try:
            return M.run_pipeline([], 0)
        finally:
            M.call = orig

    def _base(self, over: dict | None = None):
        r = {
            ("GET", "/api/status"): (200, '{"mock_mode": true}'),
            ("POST", "/api/distill"): (200, '{"job": "aaa"}'),
            ("GET", "/api/jobs/aaa"): (200, '{"status": "done"}'),
            ("GET", "/api/genome/aaa"): (200, '{"system_prompt": "' + "x" * 80
                                         + '", "persona_genes": ["a"],'
                                           ' "workflow_genes": [],'
                                           ' "source_fingerprint": "fp1"}'),
            ("GET", "/api/report/aaa"): (200, '{"final_score": 8.4}'),
            ("GET", "/api/runs"): (200, '{"runs": []}'),
            ("POST", "/api/chat"): (200, '{"reply": "你好"}'),
        }
        if over:
            r.update(over)
        return r

    def test_empty_genome_rejected(self):
        """只有键名、没有内容的基因组必须判失败。"""
        passed, total = self._run(self._base({
            ("GET", "/api/genome/aaa"): (200, '{"system_prompt": "",'
                                              ' "persona_genes": []}')}))
        self.assertLess(passed, total)

    def test_constant_genome_rejected(self):
        """两份不同输入得到同一指纹，说明产物与输入无关，必须判失败。

        这是"假功能"最典型的形态：无论喂什么都吐同一份东西。
        故这里让任务编号、提示词长度、评分全都不同，唯独指纹相同——
        若判定仍然通过，说明这一维根本没被验到。
        """
        seq = {"n": 0}

        def genome(ch: str, fp: str) -> tuple[int, str]:
            return 200, json.dumps({
                "system_prompt": ch * 80, "persona_genes": [ch],
                "workflow_genes": [], "source_fingerprint": fp})

        def stub(method, port, path, payload):
            key = (method, path.split("?")[0])
            table = {
                ("GET", "/api/status"): (200, '{"mock_mode": true}'),
                ("GET", "/api/jobs/job1"): (200, '{"status": "done"}'),
                ("GET", "/api/jobs/job2"): (200, '{"status": "done"}'),
                ("GET", "/api/genome/job1"): genome("x", "fp1"),
                ("GET", "/api/genome/job2"): genome("y", "fp1"),
                ("GET", "/api/report/job1"): (200, '{"final_score": 8.4}'),
                ("GET", "/api/report/job2"): (200, '{"final_score": 8.5}'),
                ("GET", "/api/runs"): (200, '{"runs": [{"id": "job1"},'
                                            ' {"id": "job2"}]}'),
                ("GET", "/api/compare"): (200, '{"comparable": true,'
                                               ' "prev_score": 8.4,'
                                               ' "curr_score": 8.5}'),
                ("POST", "/api/chat"): (200, '{"reply": "你好"}'),
            }
            if key == ("POST", "/api/distill"):
                seq["n"] += 1
                return 200, json.dumps({"job": f"job{seq['n']}"})
            return table.get(key, (404, "{}"))

        orig, M.call = M.call, stub
        try:
            passed, total = M.run_pipeline([], 0)
        finally:
            M.call = orig
        self.assertEqual(total - 1, passed, passed)

    def test_missing_score_rejected(self):
        passed, total = self._run(self._base({
            ("GET", "/api/report/aaa"): (200, '{"generation": 1}')}))
        self.assertLess(passed, total)

    def test_real_model_mode_not_silently_passed(self):
        """已配置真实模型时不得当作已验收：跑真实调用会产生费用。"""
        passed, total = self._run(self._base({
            ("GET", "/api/status"): (200, '{"mock_mode": false}')}))
        self.assertEqual(0, passed)
        self.assertEqual(1, total)
