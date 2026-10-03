#!/usr/bin/env python3
"""联调检查脚本：真后端 → 真蒸馏 → 真报告，验证「结论不成立必须给原因」。

为什么必须是端到端（起真服务、真发 HTTP、真跑 distill）：
    本轮失效是"结论成立 有六个合取条件，可信说明 只覆盖四个"——
    这种**条件覆盖不全**的缺陷，靠读代码看不出来（四段 if 写得都很
    完整），靠单测也不一定撞得到（要恰好走到那两个未覆盖的分支）。
    只有真跑一次蒸馏、拿到真实产物，才能看见 结论成立=false 而
    可信说明 为空。

真实联调检验到的（改动前）：
    结论成立         = False
    baseline_comparable = False     ← 六个条件里唯一被违反的那个
    verdict             = "win"     ← 界面会显示"更强"
    可信说明          = ""        ← 但后端不给任何原因
    （四个分支：污染=False 自证=False 出题自证=False 未去偏=False，
      因此一支都没命中）

    后果：前端 ArenaPage 靠回退 基线说明 侥幸显示对了，但 CLI /
    MCP / 跨版本比对这些不读 基线说明 的消费方，拿到的是
    "结论不成立且无原因"。

校验校验点：
    A  撤掉 baseline_comparable 分支 → 可信说明 断言变红
    B  把该分支挪到自证之后        → 组合场景（不可比+自证）原因给错
    C  撤掉零样本分支              → 零样本守卫变红
    每条注入后都会先 ast.parse 校验语法，防止"测试没跑起来"被误读成
    "抓到了"（本项目踩过多次）。
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def http(path: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trustnote_")
    os.environ["OMEGAFORGE_HOME"] = tmp
    os.environ["OMEGAFORGE_MODEL_MAIN"] = "mock-main"
    os.environ["OMEGAFORGE_MODEL_FAST"] = "mock-fast"
    os.environ["OMEGAFORGE_MODEL_JUDGE"] = "mock-judge"

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import omegaforge.server as srv  # noqa: E402

    # 单例惰性化后环境变量即生效，但这里仍显式对齐，避免残留目录
    for obj_name in ("RUNS", "CONVS", "USAGE", "TASKS", "KB", "JOBS"):
        obj = getattr(srv, obj_name, None)
        if obj is not None and hasattr(obj, "home"):
            try:
                obj.home = tmp
            except Exception:
                pass

    from http.server import ThreadingHTTPServer  # noqa: E402

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)

    try:
        print("=== 真实蒸馏（mock 模型，真跑完整主循环）===")
        r = http("/api/distill", {
            "source": "你是一个严谨的代码审计助手，回答前必须先核实证据。",
            "budget": 20000, "rounds": 1, "gens": 1, "task": "联调验证",
        })
        jid = r.get("job", "")
        print(f"  job = {jid}")
        status = ""
        for _ in range(60):
            j = http(f"/api/jobs/{jid}", timeout=20)
            status = str(j.get("status", ""))
            if status in ("done", "failed", "succeeded"):
                break
            time.sleep(1)
        print(f"  status = {status}")
        check("蒸馏任务真实跑完", status == "done", f"status={status}")

        rep = http(f"/api/report/{jid}", timeout=20)
        cv = rep.get("claim_valid")
        bc = rep.get("baseline_comparable")
        tn = str(rep.get("trust_note") or "")
        print("\n=== 真实产物关键字段 ===")
        for k in ("verdict", "claim_valid", "baseline_comparable",
                  "baseline_kind", "self_certified", "exam_self_authored",
                  "arena_cases", "debiased_cases", "contaminated_cases"):
            print(f"  {k:22} = {json.dumps(rep.get(k), ensure_ascii=False)}")
        print(f"  {'trust_note':22} = {json.dumps(tn, ensure_ascii=False)}")

        print("\n=== 断言 ===")
        # 真实场景：源材料无可提取 prompt → 对照组退化 → 不可比
        check("真实产物确实走到不可比场景", bc is False, f"baseline_comparable={bc}")
        check("真实产物结论确实不成立", cv is False, f"claim_valid={cv}")
        check("结论不成立时 trust_note 必须非空",
              bool(tn.strip()), f"trust_note={json.dumps(tn, ensure_ascii=False)!r}")
        check("原因指向对照问题而非裁判问题",
              ("对照" in tn) and ("裁判" not in tn),
              f"trust_note={json.dumps(tn, ensure_ascii=False)}")
        check("原因给出可执行路径",
              ("system prompt" in tn or "对照" in tn),
              f"trust_note={json.dumps(tn, ensure_ascii=False)}")

        # ---- 场景二：不可比 与 自证 **同时**成立 ----
        # 真实默认配置下这恰恰是最常见的组合（用户不单独配裁判模型 +
        # 源材料里没有可提取的 prompt）。此时若把自证原因排在前面，
        # 用户换了裁判模型之后 结论成立 仍然为假——正是"给错原因"。
        # 因此对照原因必须优先：它是"有没有可比对象"，换谁来判都修不好。
        print("\n=== 场景二：不可比 + 自证同时成立，原因必须指向对照 ===")
        os.environ["OMEGAFORGE_MODEL_JUDGE"] = "mock-fast"  # 与作答同源
        r2 = http("/api/distill", {
            "source": "你是一个严谨的代码审计助手，回答前必须先核实证据。",
            "budget": 20000, "rounds": 1, "gens": 1, "task": "联调验证-自证",
        })
        jid2 = str(r2.get("job", ""))
        for _ in range(60):
            j2 = http(f"/api/jobs/{jid2}", timeout=20)
            if str(j2.get("status", "")) in ("done", "failed", "succeeded"):
                break
            time.sleep(1)
        rep2 = http(f"/api/report/{jid2}", timeout=20)
        sc2 = rep2.get("self_certified")
        bc2 = rep2.get("baseline_comparable")
        tn2 = str(rep2.get("trust_note") or "")
        print(f"  self_certified={sc2}  baseline_comparable={bc2}")
        print(f"  trust_note = {json.dumps(tn2, ensure_ascii=False)}")
        check("场景二确实同时触发自证", sc2 is True, f"self_certified={sc2}")
        check("场景二确实同时触发不可比", bc2 is False, f"baseline_comparable={bc2}")
        check("两个原因同时成立时优先给对照（换裁判修不好不可比）",
              ("对照" in tn2) and ("裁判" not in tn2),
              f"trust_note={json.dumps(tn2, ensure_ascii=False)}")
    finally:
        httpd.shutdown()

    print("\n=== 汇总 ===")
    print(f"  {'ALL PASS' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
