#!/usr/bin/env python3
"""真浏览器交互审计：真鼠标点击、真键盘输入、真后端校验。

与既有渲染守卫的区别
--------------------
渲染守卫（render_real_pages）跑在 jsdom 里：jsdom 不做布局与绘制，
所以 `element.click()` 永远"点得到"——元素被别的元素盖住、位置算错、
尺寸为 0 导致实际点不到，在 jsdom 里一条都测不出来。

本脚本用 Chrome 的调试协议派发**真实鼠标事件**到元素的真实坐标：
先取 getBoundingClientRect，再 Input.dispatchMouseEvent。若元素被遮挡、
坐标为负、尺寸为 0，点击就会落到别处，断言失败。键盘输入走
Input.insertText，同样触发真实的 input 事件，能验到受控组件是否真的收值。

另一个区别是**双向判据**：界面上出现了还不够，还要真查后端接口，
确认数据真的落库。只有界面绿、后端查不到，说明是前端自嗨。

用法：python3 probes/frontend_render/audit_interaction.py
退出码：0=全过，1=有失败。
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
PORT = int(os.environ.get("CDP_PORT", "9223"))
BASE = os.environ.get("FE_BASE", "http://127.0.0.1:8080/")
BE_PORT = os.environ.get("BE_PORT", "8787")
DIST = os.environ.get("FE_DIST", "/data/workspace/fe_env/dist")
BE_CWD = os.environ.get("BE_CWD", "/data/workspace/LATEST")
DATA_HOME = os.environ.get("OMEGAFORGE_HOME", "/tmp/interaction_home")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_devtools(port, timeout=40):
    for _ in range(timeout * 2):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    raise SystemExit("浏览器调试端口未就绪")


def js(cdp, expr):
    r = cdp.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=False)
    res = r.get("result", {})
    if res.get("subtype") == "error":
        raise RuntimeError(res.get("description") or res.get("value"))
    return res.get("value")


def rect_of(cdp, selector_js):
    """取元素真实矩形并滚动进视口，同时打一个稳定标记。

    为什么要打标记：点击之后元素本身常会变化（文本改了、title 变了、
    按钮置灰），命中校验若重新执行一遍选择器就会找不到，被误判成 gone。
    """
    js(cdp, """(() => {
      document.querySelectorAll('[data-probe-click]')
        .forEach(e => e.removeAttribute('data-probe-click'));
    })()""")
    return js(cdp, """
    (() => {
      const el = %s;
      if (!el) return null;
      el.scrollIntoView({block: 'center', inline: 'center'});
      el.setAttribute('data-probe-click', '1');
      const r = el.getBoundingClientRect();
      return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height};
    })()
    """ % selector_js)


def real_click(cdp, selector_js, label):
    """用真实鼠标事件点击元素中心。返回是否命中。"""
    rect = rect_of(cdp, selector_js)
    if not rect:
        return False, f"{label}：找不到元素"
    if rect["w"] <= 0 or rect["h"] <= 0:
        return False, f"{label}：元素尺寸为 0（{rect['w']}x{rect['h']}）"
    if rect["x"] < 0 or rect["y"] < 0:
        return False, f"{label}：元素在视口外（{rect['x']:.0f},{rect['y']:.0f}）"
    x, y = rect["x"], rect["y"]
    # 命中校验必须在点击**之前**做：点击后 React 常会重渲染替换掉整棵子树，
    # 标记随旧节点一起消失，那时再查就会误判成 gone。
    hit = js(cdp, """
    (() => {
      const el = document.querySelector('[data-probe-click]');
      if (!el) return 'gone';
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
      if (!top) return 'null';
      return (el === top || el.contains(top) || top.contains(el)) ? 'hit' : 'blocked';
    })()
    """)
    if hit != "hit":
        return False, f"{label}：点击会落空（{hit}）"
    for etype in ("mousePressed", "mouseReleased"):
        cdp.send("Input.dispatchMouseEvent", type=etype, x=x, y=y,
                 button="left", clickCount=1, buttons=1 if etype == "mousePressed" else 0)
    time.sleep(0.4)
    return True, ""


def real_type(cdp, selector_js, text, label):
    """真实点击聚焦后插入文本，触发受控组件的 input 事件。"""
    ok, why = real_click(cdp, selector_js, label)
    if not ok:
        return False, why
    cdp.send("Input.insertText", text=text)
    time.sleep(0.3)
    got = js(cdp, """
    (() => { const el = %s; return el ? String(el.value ?? '') : null; })()
    """ % selector_js)
    if got != text:
        return False, f"{label}：输入框实际取到「{got}」，与输入不符"
    return True, ""


def nav_btn(label):
    return """Array.from(document.querySelectorAll('nav button'))
                 .find(e => (e.textContent||'').includes(%s))""" % json.dumps(label)


def btn_by_text(text):
    return """Array.from(document.querySelectorAll('button'))
                 .find(e => (e.textContent||'').includes(%s) && !e.disabled)""" % json.dumps(text)


def inp_by_placeholder(ph):
    return """document.querySelector('input[placeholder=%s]')""" % json.dumps(ph)


def body_text(cdp):
    return js(cdp, "document.body.innerText.slice(0, 4000)") or ""


def api(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{BE_PORT}{path}", timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"__error__": str(e)}


def api_post(path, body):
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{BE_PORT}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"__error__": str(e)}


def ensure_run(timeout=150):
    """准备真实运行产物：目录里没有就真跑一次蒸馏。

    为什么必须单独判定这一步：本审计存在的意义是验**有内容时**能不能用。
    蒸馏请求没被受理、任务以失败结束、或完成后列表仍为空，这三种情况下
    后续断言都会落在空页面上——而空页面同样能渲染、能点击，若干断言照样
    通过，审计会静默退化成首启审计的重复，真正该覆盖的那一半没人验。

    返回 (是否就绪, 说明)。失败时说明指向准备阶段本身，而不是让人误以为
    是界面列表渲染坏了。
    """
    runs = api("/api/runs").get("runs") or []
    if runs:
        return True, f"复用目录中已有的 {len(runs)} 条运行记录"

    r = api_post("/api/distill", {
        "source": "你是一个严谨的代码审计助手，回答前必须先核实证据。",
        "budget": 20000, "rounds": 1, "gens": 1,
        "task": "真浏览器交互验证",
    })
    if "__error__" in r:
        return False, f"蒸馏请求未被受理：{r.get('__error__')}"
    jid = str(r.get("job") or "")
    if not jid:
        return False, f"蒸馏请求未返回任务编号：{str(r)[:120]}"

    for _ in range(timeout):
        j = api(f"/api/jobs/{jid}")
        if "__error__" in j:
            return False, f"查询任务状态失败：{j.get('__error__')}"
        st = str(j.get("status") or "")
        if st in ("done", "succeeded"):
            break
        if st in ("failed", "error", "cancelled", "interrupted"):
            return False, f"蒸馏任务以 {st} 结束，未产出可验证的运行记录"
        time.sleep(1)
    else:
        return False, f"蒸馏在 {timeout} 秒内未结束（最后状态 {st!r}）"

    runs = api("/api/runs").get("runs") or []
    if not runs:
        return False, f"蒸馏状态为 {st}，但运行列表仍为空"
    return True, f"已造出运行产物，共 {len(runs)} 条"


def main():
    if not CHROME:
        raise SystemExit("找不到浏览器")
    shutil.rmtree(DATA_HOME, ignore_errors=True)
    os.makedirs(DATA_HOME, exist_ok=True)

    env = dict(os.environ, OMEGAFORGE_HOME=DATA_HOME)
    static = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8080", "--directory", DIST],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = subprocess.Popen(
        [sys.executable, "-m", "omegaforge.server", "--port", BE_PORT],
        cwd=BE_CWD, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/", timeout=2).read()
            urllib.request.urlopen(f"http://127.0.0.1:{BE_PORT}/api/runs", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        static.terminate(); backend.terminate()
        raise SystemExit("静态服务或后端未就绪")

    profile = "/tmp/chrome_profile_interact"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         "--window-size=1440,900", f"--user-data-dir={profile}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        wait_devtools(PORT)
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/list", timeout=5).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=1440, height=900, deviceScaleFactor=1, mobile=False)

        # ---------- 0. 准备真实数据：没有运行记录就真跑一次蒸馏 ----------
        # 运行记录页要能点开，前提是目录里确实有产物。靠外部数据目录不可靠
        # （沙盒重置就没了），所以这里自己造：真发蒸馏请求，真等到跑完。
        ok_prep, why = ensure_run(timeout=120)
        check("准备阶段：目录里有可验证的运行记录", ok_prep, why)
        if not ok_prep:
            print("\n=== 汇总 ===")
            print("准备阶段未就绪，后续断言会落在空页面上，不再继续")
            return 1

    # ---------- 1. 真鼠标点击导航 ----------
        print("=== 导航点击（真实鼠标事件）===")
        for label in ["待办", "知识库", "运行记录", "设置"]:
            cdp.send("Page.navigate", url=BASE)
            time.sleep(2.2)
            ok, why = real_click(cdp, nav_btn(label), f"导航「{label}」")
            if ok:
                time.sleep(1.2)
                cur = js(cdp, """
                (() => { const b = %s;
                  return b ? String(b.getAttribute('aria-current') || '') : ''; })()
                """ % nav_btn(label))
                check(f"导航「{label}」可点击且切换生效", cur == "page", f"aria-current={cur!r}")
            else:
                check(f"导航「{label}」可点击且切换生效", False, why)

        # ---------- 2. 待办：真新增 + 真落库 ----------
        print("\n=== 待办：新增并落库 ===")
        cdp.send("Page.navigate", url=BASE)
        time.sleep(2.2)
        real_click(cdp, nav_btn("待办"), "导航待办")
        time.sleep(1.2)
        marker = "真浏览器交互验证任务"
        ok, why = real_type(cdp, inp_by_placeholder("要做的事"), marker, "待办输入框")
        check("待办输入框能真实收值", ok, why)
        ok, why = real_click(cdp, btn_by_text("添加"), "「添加」按钮")
        check("「添加」按钮可真实点击", ok, why)
        time.sleep(1.5)
        txt = body_text(cdp)
        check("界面出现新任务", marker in txt)
        data = api("/api/tasks/list")
        items = data.get("tasks") or data.get("items") or []
        check("后端确有该任务", any(marker in str(i) for i in items),
              f"后端返回 {len(items)} 条")

        # ---------- 3. 待办：真标记完成 ----------
        print("\n=== 待办：标记完成 ===")
        done_js = """(() => {
          const rows = Array.from(document.querySelectorAll('div'))
            .filter(d => (d.textContent||'').includes(%s));
          for (const r of rows) {
            const b = Array.from(r.querySelectorAll('button'))
              .find(x => (x.getAttribute('title')||'') === '标记完成');
            if (b) return b;
          }
          return null;
        })()""" % json.dumps(marker)
        ok, why = real_click(cdp, done_js, "「标记完成」按钮")
        check("「标记完成」按钮可真实点击", ok, why)
        time.sleep(1.5)
        # /api/tasks/list 默认 scope=pending，完成后自然不在默认视图里；
        # 断言必须查全量，否则会把"正确行为"误判成没落库。
        data = api("/api/tasks/list?scope=all")
        items = data.get("items") or []
        hit = next((i for i in items if marker in str(i)), None)
        check("后端标记该任务为已完成", bool(hit) and bool(
            (hit.get("done") if isinstance(hit, dict) else False)),
            f"命中={str(hit)[:80] if hit else '无'}")

        # ---------- 4. 知识库：真新增 + 必填校验 + 真检索 ----------
        print("\n=== 知识库：新增、必填校验、检索 ===")
        cdp.send("Page.navigate", url=BASE)
        time.sleep(2.2)
        real_click(cdp, nav_btn("知识库"), "导航知识库")
        time.sleep(1.2)
        kb_title = "真浏览器知识标题"
        kb_body = "真浏览器知识正文"
        ok, _ = real_type(cdp, inp_by_placeholder("标题"), kb_title, "标题输入框")
        check("知识库标题框能真实收值", ok)
        ok, _ = real_click(
            cdp, """document.querySelector('textarea[placeholder="内容"]')""", "内容输入框")
        if ok:
            cdp.send("Input.insertText", text=kb_body)
            time.sleep(0.3)
        check("知识库内容框能真实收值", ok)

        # 先只留标题、清空内容，验证必填校验在真浏览器里真的拦住并给出中文提示
        js(cdp, """(() => {
          const t = document.querySelector('textarea[placeholder="内容"]');
          if (t) { t.focus(); }
        })()""")
        for k, code, vk in (("a", "KeyA", 65), ("Delete", "Delete", 46)):
            cdp.send("Input.dispatchKeyEvent", type="keyDown", key=k,
                     code=code, windowsVirtualKeyCode=vk, modifiers=2)
            cdp.send("Input.dispatchKeyEvent", type="keyUp", key=k,
                     code=code, windowsVirtualKeyCode=vk, modifiers=2)
        time.sleep(0.3)
        cleared = js(cdp, """(() => {
          const t = document.querySelector('textarea[placeholder="内容"]');
          return t ? String(t.value || '') : null; })()""")
        check("内容框可被真实键盘清空", cleared == "", f"清空后取到「{cleared}」")
        ok, why = real_click(cdp, btn_by_text("添加"), "知识库「添加」（内容为空）")
        check("缺内容时「添加」仍可点击", ok, why)
        time.sleep(1.2)
        txt = body_text(cdp)
        check("缺内容时不落库", not any(kb_title in str(i) for i in
              (lambda d: (d.get("docs") or []) if isinstance(d, dict) else d)(api("/api/kb/list"))))
        check("缺内容时给出中文提示而非泛化失败",
              ("请填写" in txt or "必填" in txt or "不能为空" in txt),
              f"页面未见必填提示")

        # 补全内容后真新增
        ok, _ = real_click(
            cdp, """document.querySelector('textarea[placeholder="内容"]')""", "内容输入框")
        cdp.send("Input.insertText", text=kb_body)
        time.sleep(0.3)
        ok, why = real_click(cdp, btn_by_text("添加"), "知识库「添加」")
        check("知识库「添加」可真实点击", ok, why)
        time.sleep(1.5)
        kb_data = api("/api/kb/list")
        lst = kb_data.get("docs") if isinstance(kb_data, dict) else kb_data
        check("知识库后端确有该条目", any(kb_title in str(i) for i in (lst or [])),
              f"后端返回 {len(lst or [])} 条")

        # 检索：真填搜索框、真点检索，断言结果里命中刚写入的条目
        ok, why = real_type(cdp, inp_by_placeholder("检索知识库 / 记忆"), kb_title, "检索输入框")
        check("检索框能真实收值", ok, why)
        ok, why = real_click(cdp, btn_by_text("检索"), "「检索」按钮")
        check("「检索」按钮可真实点击", ok, why)
        time.sleep(1.5)
        txt = body_text(cdp)
        check("检索结果命中刚写入的条目", kb_title in txt,
              f"页面文本片段：{txt[:200]!r}")
        # 界面没命中时，分清是"前端没渲染"还是"后端没检索到"
        if kb_title not in txt:
            for key in ("q", "query"):
                r = api_post("/api/kb/search", {key: kb_title})
                got = r.get("results") or []
                print(f"    · 后端 /api/kb/search 用 {key!r}：命中 {len(got)} 条 "
                      f"{[x.get('title') for x in got][:3]}")

        # ---------- 5. 运行记录：真点开详情 ----------
        print("\n=== 运行记录：点开详情 ===")
        cdp.send("Page.navigate", url=BASE)
        time.sleep(2.2)
        real_click(cdp, nav_btn("运行记录"), "导航运行记录")
        time.sleep(1.2)
        # 运行记录页是列表，不是下拉：列表项 button 的 title 就是运行编号
        run_js = """Array.from(document.querySelectorAll('button'))
                      .find(b => /^[0-9a-f]{8,}$/.test(b.getAttribute('title') || ''))"""
        run_id = js(cdp, "(() => { const b = %s; return b ? b.getAttribute('title') : null; })()" % run_js)
        if run_id:
            ok, why = real_click(cdp, run_js, "首条运行记录")
            check("运行记录项可真实点击", ok, why)
            time.sleep(1.0)
            cur = js(cdp, "(() => { const b = %s; return b ? String(b.getAttribute('aria-current')||'') : ''; })()" % run_js)
            check("运行记录切换生效", cur == "true", f"aria-current={cur!r}")
            # 详情是异步拉的，点完立刻断言会撞上骨架屏；轮询到出内容为止
            txt = ""
            for _ in range(30):
                txt = body_text(cdp)
                if "裁决" in txt or "蒸馏完成" in txt:
                    break
                time.sleep(1.0)
            check("详情区渲染出该运行的真实产物",
                  ("裁决" in txt or "蒸馏完成" in txt),
                  f"文本片段：{txt[300:600]!r}")
            check("详情区显示该运行编号", run_id[:8] in txt,
                  f"运行编号 {run_id[:8]}")
            # 详情里有数组/对象字段，标题原先直接把键名大写甩给用户
            leaking = [w for w in ("JUDGE REASONS", "EVOLUTION NOTES",
                                   "ARENA CASES", "SOURCE SIGNALS")
                       if w in txt]
            check("详情区不出现大写英文键名", not leaking, f"出现 {leaking}")
        else:
            check("运行记录列表存在", False, "未找到带运行编号的列表项")

        passed = sum(1 for _, ok, _ in RESULTS if ok)
        total = len(RESULTS)
        print(f"\n=== 汇总 ===\n  {passed}/{total} 通过")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAIL {name} —— {detail}")
        return 0 if passed == total else 1
    finally:
        for pr in (proc, static, backend):
            try:
                pr.terminate()
                pr.wait(timeout=8)
            except Exception:
                try:
                    pr.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
