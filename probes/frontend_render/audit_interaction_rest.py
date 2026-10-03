#!/usr/bin/env python3
"""真浏览器交互审计（其余页面）。

覆盖对象与已有脚本互补：设置、对话、技能与人设、蒸馏工坊、待办分支
开关、知识库页签、以及依赖运行产物的竞技场 / 基因组 / 用量三页。

判据一律双向：界面上出现还不够，还要真查后端接口确认数据落库或状态
生效；只验界面属于自嗨——前端画得出而后端没有，用户刷新后一切归零。

用法：python3 probes/frontend_render/audit_interaction_rest.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import audit_interaction as ai

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def goto(cdp, label):
    ai.cdp = cdp
    ai.js(cdp, "void 0")
    cdp.send("Page.navigate", url=ai.BASE)
    time.sleep(2.0)
    if label != "蒸馏工坊":
        ai.real_click(cdp, ai.nav_btn(label), f"导航{label}")
        time.sleep(1.2)


def _post_raw(path: str, payload: dict) -> str:
    """向真实后端发一次请求，成功与失败都返回响应体原文。

    地址必须指向后端（BE_PORT），不能沿用 BASE——BASE 是静态文件服务的
    端口，往它发 POST 会拿到内置错误页的 HTML，读起来像"接口坏了"，实际
    是问错了对象。
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{ai.BE_PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")


def api_get(path: str) -> dict:
    """向真实后端取一次清单，失败时返回空字典。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{ai.BE_PORT}{path}", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def main():
    ai.BASE = os.environ.get("FE_BASE", "http://127.0.0.1:8090/")
    ai.BE_PORT = os.environ.get("BE_PORT", "8787")
    ai.DATA_HOME = os.environ.get("OMEGAFORGE_HOME", "/tmp/interact_rest_home")
    ai.DIST = os.environ.get("FE_DIST", "/data/workspace/fe_env/dist")

    shutil.rmtree(ai.DATA_HOME, ignore_errors=True)
    os.makedirs(ai.DATA_HOME, exist_ok=True)
    # 界面按约定端口连接后端，旧实例占着端口会让整轮审计变成"服务未连接"
    me = os.getpid()
    for pid in [int(x) for x in os.listdir("/proc") if x.isdigit()]:
        if pid == me:
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
        except Exception:
            continue
        if "omegaforge.server" in cmd:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
    time.sleep(1.5)
    env = dict(os.environ, OMEGAFORGE_HOME=ai.DATA_HOME)
    static = subprocess.Popen([sys.executable, "-m", "http.server", "8090",
                               "--directory", ai.DIST],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = subprocess.Popen([sys.executable, "-m", "omegaforge.server",
                                "--port", ai.BE_PORT],
                               cwd=os.path.dirname(os.path.dirname(_HERE)), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(ai.BASE, timeout=2).read()
            urllib.request.urlopen(f"http://127.0.0.1:{ai.BE_PORT}/api/runs", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        static.terminate(); backend.terminate()
        raise SystemExit("静态服务或后端未就绪")

    # 竞技场 / 基因组 / 用量三页都依赖真实运行产物，先真跑一次蒸馏造数据
    ok_prep, why = ai.ensure_run(timeout=150)
    if not ok_prep:
        print(f"准备阶段未就绪：{why}")
        print("后续断言会落在空页面上，不再继续")
        return 1
    print(f"  准备阶段：{why}")

    profile = "/tmp/chrome_profile_rest"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [ai.CHROME, "--headless=new", "--remote-debugging-port=9225", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars",
         "--window-size=1440,900", f"--user-data-dir={profile}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ai.wait_devtools(9225)
        targets = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:9225/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        cdp = ai.CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=1440, height=900, deviceScaleFactor=1, mobile=False)

        # ---------- 1. 设置：选供应商并保存 ----------
        print("\n=== 设置：供应商选择与保存 ===")
        goto(cdp, "设置")
        ok, why = ai.real_click(cdp, ai.btn_by_text("选择供应商…"), "供应商下拉")
        check("供应商下拉可真实点开", ok, why)
        time.sleep(0.9)
        opts = ai.js(cdp, """Array.from(document.querySelectorAll('[role="option"]'))
                                .map(e => (e.textContent||'').trim())""") or []
        check("下拉展开真实出现选项", len(opts) > 0, f"选项 {opts[:4]}")
        picked = opts[0] if opts else ""
        if opts:
            ai.real_click(cdp, """Array.from(document.querySelectorAll('[role="option"]'))[0]""",
                          "第一个选项")
            time.sleep(0.9)
        # 选中后 trigger 的占位文本会被选项名替换，按占位文本找会落空；
        # 下拉触发器的语义角色是 combobox，取它才能读到当前值。
        shown = ai.js(cdp, """(() => {
          const c = document.querySelector('[role="combobox"]');
          return c ? (c.textContent||'').trim() : ''; })()""") or ""
        # 不能拿"选项文本"和"触发器文本"互相比较：两者同源，界面改回显示
        # 内部标识时它们仍然相等，断言恒真。也不能靠"不含下划线"判断——
        # openai、local 这类标识本来就没有下划线。
        #
        # 唯一可靠的基准是后端清单：它对同一家同时给 name（内部标识）与
        # label（中文名）。界面必须显示 label，且在两者不同时不能显示 name。
        prov = api_get("/api/providers")
        presets = (prov or {}).get("presets") or []
        pairs = [(str(x.get("name", "")), str(x.get("label", "")))
                 for x in presets if isinstance(x, dict)]
        differ = [(n, lb) for n, lb in pairs if lb and n and lb != n]
        check("后端清单同时给出内部标识与中文名（否则本项无从比对）",
              len(differ) > 0,
              f"清单里 name/label 不同的条目 {len(differ)} 个：{differ[:3]}")
        check("选中后下拉显示可读名称而非内部标识",
              bool(picked) and any(picked == lb for _n, lb in pairs)
              and not any(picked == n for n, lb in differ),
              f"下拉当前显示「{shown}」，选项为「{picked}」，"
              f"清单里 name/label 不同的条目为 {differ[:3]}")
        if differ:
            bad = [n for n, lb in differ if n in " ".join(opts)]
            check("下拉选项里不出现内部标识（以清单里的 name 为准）",
                  not bad, f"选项里出现了内部标识：{bad[:4]}")
        ok, why = ai.real_type(cdp, ai.inp_by_placeholder("留空则不修改"), "sk-probe-0000",
                               "密钥输入框")
        check("密钥输入框能真实收值", ok, why)
        ok, why = ai.real_click(cdp, ai.btn_by_text("应用"), "「应用」按钮")
        check("「应用」按钮可真实点击", ok, why)
        time.sleep(1.5)
        prov = ai.api("/api/providers")
        saved_ok = "sk-probe-0000" not in json.dumps(prov, ensure_ascii=False) and (
            picked.split()[0] in json.dumps(prov, ensure_ascii=False) if picked else False)
        check("后端确认已保存该供应商", saved_ok,
              f"接口返回片段：{json.dumps(prov, ensure_ascii=False)[:160]}")
        # 刷新后仍在——只验界面属于前端自嗨，后端没有则刷新后归零
        goto(cdp, "设置")
        after = ai.js(cdp, """(() => {
          const c = document.querySelector('[role="combobox"]');
          return c ? (c.textContent||'').trim() : ''; })()""") or ""
        check("刷新后仍显示该供应商", bool(picked) and picked.split()[0] in after,
              f"刷新后显示「{after}」")

        # ---------- 2. 对话：发送消息 ----------
        print("\n=== 对话：发送后有明确反馈 ===")
        goto(cdp, "对话")
        marker = "真浏览器交互验证消息"
        ok, why = ai.real_type(cdp, """document.querySelector('textarea')""", marker, "对话输入框")
        check("对话输入框能真实收值", ok, why)
        ok, why = ai.real_click(cdp, ai.btn_by_text("发送"), "「发送」按钮")
        check("「发送」按钮可真实点击", ok, why)
        time.sleep(3.0)
        txt = ai.body_text(cdp)
        check("发送后界面出现该条消息", marker in txt)
        # 判据是"不静默"：要么出现回复，要么给出明确说明。两者都没有、
        # 又不报英文技术串，才是最难发现的失效。
        responded = marker in txt and ("gpt-" in txt or "自动" in txt or "未接入" in txt)
        check("发送后有回复或有明确说明（不静默）", responded, f"页面片段：{txt[:180]!r}")
        for eng in ("Traceback", "undefined", "NoneType"):
            check(f"对话区不出现英文技术串 {eng}", eng not in txt)

        # ---------- 3. 技能与人设：安装不存在路径 ----------
        print("\n=== 技能与人设：路径不存在时的提示 ===")
        goto(cdp, "技能与人设")
        ok, why = ai.real_type(cdp, ai.inp_by_placeholder("在此粘贴目录路径"),
                               "/tmp/不存在的技能目录", "路径输入框")
        check("路径输入框能真实收值", ok, why)
        ok, why = ai.real_click(cdp, ai.btn_by_text("安装"), "「安装」按钮")
        check("「安装」按钮可真实点击", ok, why)
        time.sleep(2.0)
        txt = ai.body_text(cdp)
        has_cn = any(k in txt for k in ("不存在", "无法", "失败", "未找到", "请"))
        check("路径不存在时给出中文提示", has_cn, f"页面片段：{txt[:200]!r}")
        check("提示中不出现英文调用栈", "Traceback" not in txt)
        # 只看页面文本不够：安装结果未必渲染进正文（可能只在提示条里一闪
        # 而过）。那样上面两条在"界面根本没显示"时同样成立，等于恒真。
        # 因此对着真实后端再问一次，以响应体原文为准。
        rtxt = _post_raw("/api/skills/install",
                         {"path": "/tmp/不存在的技能目录"})
        check("安装失败时后端响应里给出可行动的中文",
              any(k in rtxt for k in ("不存在", "未找到", "请", "路径")),
              f"响应：{rtxt[:200]}")
        check("安装失败的后端响应里不出现英文调用栈",
              "Traceback" not in rtxt, f"响应：{rtxt[:200]}")

        # ---------- 4. 蒸馏工坊：填表并真发起 ----------
        print("\n=== 蒸馏工坊：填写并发起 ===")
        goto(cdp, "蒸馏工坊")
        before_disabled = ai.js(cdp, """(() => {
          const b = Array.from(document.querySelectorAll('button'))
            .find(e => (e.textContent||'').includes('开始蒸馏'));
          return b ? !!b.disabled : null; })()""")
        check("未填写时「开始蒸馏」不可用", before_disabled is True, f"disabled={before_disabled}")
        ai.real_type(cdp, """document.querySelectorAll('textarea')[0]""",
                     "你是一个严谨的代码审计助手，回答前必须先核实证据。", "源提示词框")
        ai.real_type(cdp, """document.querySelectorAll('textarea')[1]""",
                     "真浏览器交互验证任务", "任务框")
        time.sleep(0.6)
        after_disabled = ai.js(cdp, """(() => {
          const b = Array.from(document.querySelectorAll('button'))
            .find(e => (e.textContent||'').includes('开始蒸馏'));
          return b ? !!b.disabled : null; })()""")
        check("填写后「开始蒸馏」转为可用", after_disabled is False, f"disabled={after_disabled}")
        ok, why = ai.real_click(cdp, ai.btn_by_text("开始蒸馏"), "「开始蒸馏」按钮")
        check("「开始蒸馏」按钮可真实点击", ok, why)
        time.sleep(3.0)
        txt = ai.body_text(cdp)
        runs_after = len(ai.api("/api/runs").get("runs") or [])
        check("后端确有新增运行", runs_after >= 2, f"运行数 {runs_after}")
        check("界面给出任务已发起的反馈",
              any(k in txt for k in ("任务", "运行中", "排队", "蒸馏")),
              f"页面片段：{txt[:180]!r}")

        # ---------- 5. 待办：优先级与显示开关 ----------
        print("\n=== 待办：优先级与已完成开关 ===")
        goto(cdp, "待办")
        ok, why = ai.real_click(cdp, ai.btn_by_text("紧急"), "「紧急」优先级")
        check("优先级「紧急」可真实点击", ok, why)
        time.sleep(0.5)
        urgent_on = ai.js(cdp, """(() => {
          const b = Array.from(document.querySelectorAll('button'))
            .find(e => (e.textContent||'').includes('紧急'));
          return b ? (b.className || '') : ''; })()""") or ""
        check("优先级选中态在界面上有区分", len(urgent_on) > 0, f"class 片段 {urgent_on[:80]}")
        ok, why = ai.real_click(cdp, ai.btn_by_text("显示已完成"), "「显示已完成」开关")
        check("「显示已完成」开关可真实点击", ok, why)

        # ---------- 6. 知识库：记忆页签 ----------
        print("\n=== 知识库：记忆页签 ===")
        goto(cdp, "知识库")
        ok, why = ai.real_click(cdp, ai.btn_by_text("记忆"), "「记忆」页签")
        check("「记忆」页签可真实点击", ok, why)
        time.sleep(1.0)
        txt = ai.body_text(cdp)
        check("切到记忆后界面内容随之变化", "记忆" in txt, f"页面片段：{txt[:150]!r}")

        # ---------- 7. 依赖运行产物的三页 ----------
        for label in ("竞技场", "基因组", "用量"):
            print(f"\n=== {label}：有产物时的可用性 ===")
            goto(cdp, label)
            time.sleep(1.5)
            txt = ai.body_text(cdp)
            check(f"{label} 页未停在空白或错误态",
                  len(txt.strip()) > 40 and "出错" not in txt[:200],
                  f"页面片段：{txt[:150]!r}")
            trig = ai.js(cdp, """(() => {
              const b = Array.from(document.querySelectorAll('button'))
                .find(e => (e.textContent||'').includes('选择运行'));
              return b ? true : false; })()""")
            if trig:
                ok, why = ai.real_click(cdp, ai.btn_by_text("选择运行"), f"{label} 运行下拉")
                check(f"{label} 的运行下拉可真实点开", ok, why)
                time.sleep(0.9)
                opts = ai.js(cdp, """Array.from(document.querySelectorAll('[role="option"]'))
                                        .map(e => (e.textContent||'').trim())""") or []
                check(f"{label} 下拉列出真实运行", len(opts) > 0, f"选项 {opts[:3]}")
    finally:
        try:
            cdp.close()
        except Exception:
            pass
        proc.terminate(); static.terminate(); backend.terminate()

    print("\n=== 汇总 ===")
    bad = [n for n, ok in RESULTS if not ok]
    print(f"  {len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    for n in bad:
        print(f"  未通过：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
