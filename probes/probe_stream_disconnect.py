#!/usr/bin/env python3
"""流式对话中途断开时，落库内容是否完整。

桌面应用的真实场景：回复还在逐字推送，用户关掉窗口 / 切走页面，
连接断开。要回答三个问题：

  1. 服务端会不会崩（未兜住的 BrokenPipeError 冒泡到 handler）
  2. 对话里存下来的是不是**半截回复**
  3. 用量有没有为一条没读完的回复照常全额记账

判据不看代码注释，只看断开之后 /api/conversations/<id> 里到底有什么。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

HOME = "/tmp/probe_stream_home"
FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


# ------------------------------------------------------- 假上游（真 SSE）--

class _Up:
    """真的 SSE 上游：推 N 个 chunk，之后挂住不结束。"""

    CHUNKS = ["你", "好", "，", "这", "是", "一", "段", "完", "整", "回", "复"]

    def __init__(self, hold_after: int):
        self.hold = hold_after
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.srv.listen(8)
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
            head = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    "Cache-Control: no-cache\r\n\r\n").encode()
            c.sendall(head)
            for i, ch in enumerate(self.CHUNKS):
                obj = {"choices": [{"delta": {"content": ch}}]}
                try:
                    c.sendall(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
                except Exception:
                    return          # 客户端已断开
                if i + 1 >= self.hold:
                    # 推够 N 个就结束上游（不发 [DONE]）：模拟上游正常收尾，
                    # 而客户端早已断开。服务端此时会走完落库，再在推 done
                    # 事件时发现管道已断 —— 这才是真实的中断时序。
                    return
                time.sleep(0.05)
            c.sendall(b"data: [DONE]\n\n")
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass


def main() -> int:
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)
    os.environ["OMEGAFORGE_HOME"] = HOME
    os.environ["OMEGAFORGE_MOCK"] = "1"

    up = _Up(hold_after=6)

    import omegaforge.server as srv

    for s in (srv.KB, srv.WIKI, srv.TASKS, srv.USAGE,
              srv.PROVIDERS, srv.CONVS, srv.RUNS):
        inv = getattr(s, "invalidate", None)
        if inv is not None:
            inv()

    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    time.sleep(0.4)

    def post(path, payload):
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=30)

    def get(path):
        return json.loads(urllib.request.urlopen(base + path, timeout=10).read())

    # 上游地址只能通过**真实配置接口**写入：流式端点用的是裸 urlopen，
    # 不走 LLMClient，因此 MOCK=1 对它不生效 —— 不配置的话它会真的去连
    # api.openai.com（沙盒无外网，表现为挂到 120 秒超时再走兜底）。
    # 这一点本身就是发现：mock 环境下所有流式行为走的都不是被测路径。
    post("/api/providers/apply", {
        "name": "custom",
        "base_url": f"http://127.0.0.1:{up.port}/v1",
        "api_key": "probe-key",
        "models": {"main": "probe-main", "fast": "probe-fast",
                   "judge": "probe-judge"}}).read()

    print("=" * 70)
    print("场景：流式回复推送到一半，客户端断开")
    print("=" * 70)

    # ---- 建会话并发起流式请求，读 2 个事件后断开 ----
    with post("/api/chat/stream", {"message": "你好"}) as r:
        cid = None
        n = 0
        try:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                obj = json.loads(line[5:].strip())
                if obj.get("type") == "start":
                    cid = obj.get("conversation_id")
                n += 1
                if n >= 3:          # start + 2 个 delta
                    break
        finally:
            r.close()               # 客户端断开

    time.sleep(1.5)                 # 给服务端收尾的时间

    # ---- 1. 服务端还活着吗 ----
    alive = True
    try:
        st = get("/api/status")
    except Exception as e:
        alive = False
        st = {}
        print(f"    服务端异常：{type(e).__name__}: {e}")
    check(alive, "断开后服务端仍可响应（未崩）", str(st.get("version", "")))

    # ---- 2. 落库内容 ----
    # 列表接口只给 id/title/count，不含消息全文（检验），必须走详情接口；
    # 拿列表去判"有没有落库"会得出"完全没落库"的假结论。
    try:
        detail = get(f"/api/conversations/{cid}")
    except Exception as e:
        detail = {}
        print(f"    详情接口异常：{type(e).__name__}: {e}")
    msgs = (detail.get("conversation") or detail or {}).get("messages") or []
    if not msgs:    # 兜底：直接读磁盘，避免接口形态影响结论
        fp = os.path.join(HOME, "conversations", f"{cid}.json")
        if os.path.exists(fp):
            msgs = json.load(open(fp, encoding="utf-8")).get("messages") or []
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    saved = "".join(m.get("content", "") for m in assistant)

    full = "".join(_Up.CHUNKS)
    print(f"    会话 id={cid}")
    print(f"    assistant 消息数={len(assistant)}  落库文本={saved!r}")
    print(f"    完整应为={full!r}  已推送={full[:2]!r}")

    check(bool(assistant), "断开后存在 assistant 消息",
          "" if assistant else "（完全没有落库）")
    check(saved.startswith(full[:3]), "落库保留已推送的内容（不凭空消失）",
          f"实际={saved!r}")
    # 半截不是问题，**没标出来**才是：界面上它看起来是一条样式完好的
    # 普通回答，用户会当作完整答案，后续提问还基于这半句话继续。
    flagged = any(m.get("interrupted") for m in assistant)
    partial = saved != full
    check((not partial) or flagged, "半截回复已标记为被中断",
          f"落库={saved!r} 完整={full!r} flagged={flagged}")

    # ---- 3. 用量 ----
    try:
        usage = get("/api/usage")
        print(f"    usage keys={list(usage)[:8]}")
        check(True, "用量接口在断开后仍可读")
    except Exception as e:
        check(False, "用量接口在断开后仍可读", f"{type(e).__name__}: {e}")

    # ---- 4. 服务端是否把断开当成错误记进日志 ----
    logp = os.path.join(HOME, "logs")
    found = []
    if os.path.isdir(logp):
        for fn in os.listdir(logp):
            fp = os.path.join(logp, fn)
            if os.path.isfile(fp):
                try:
                    t = open(fp, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                for kw in ("BrokenPipe", "ConnectionReset", "断开", "管道"):
                    if kw in t:
                        found.append(f"{fn}:{kw}")
    print(f"    日志中命中：{found or '无'}")
    check(not any("BrokenPipe" in f or "ConnectionReset" in f for f in found),
          "未把客户端断开记成内部故障", str(found))

    httpd.shutdown()
    up.stop = True

    print("\n" + "=" * 70)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
