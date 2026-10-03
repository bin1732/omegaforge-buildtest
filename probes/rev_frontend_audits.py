#!/usr/bin/env python3
"""布局审计的元审计：确认审计本身在起作用。

审计报"0 处"时存在两种可能，输出上无法区分：

1. 界面确实没有问题；
2. 页面没加载出来、脚本没跑到——四类问题同样都是空列表。

只看汇总数字分辨不出这两者。因此本脚本验的是审计自身的判定能力，
而不是界面：给审计喂它应当抓到的输入，它必须报出来。

三个校验点：

A 溢出注入    在产物里放一个超出视口宽度的元素，审计必须报溢出并判未通过
B 规模自检    把规模阈值抬到不可能满足的值，审计必须判"未真正加载"，
              而不是给出四类全零的"通过"
C 基线        不注入，审计必须通过

B 是 A 之外的独立一维：它验的是"空结果会不会被当成整洁"。
只验 A 的话，规模自检被删掉也不会有任何校验点变红。

用法：python3 probes/rev_frontend_audits.py [A|B|C|all]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "probes", "frontend_render", "audit_layout.py")
DIST = os.environ.get("FE_DIST", "/data/workspace/fe_env/dist")
INDEX = os.path.join(DIST, "index.html")
BACKUP = "/tmp/rev_frontend_audits_index.html"

# 注入标记：一个远超视口宽度的元素，三种视口下都应被判为横向溢出
OVERFLOW_TAG = "rev-overflow-probe"
OVERFLOW_HTML = (
    '<div id="%s" style="width:9999px;height:24px">溢出探针</div>\n'
    % OVERFLOW_TAG
)


def heal():
    """把上一次运行遗留的注入痕迹清掉。

    本脚本若在注入与还原之间被中断，产物会停在已注入状态，此后基线恒红、
    校验失去意义。开局先无条件还原一次，让本脚本可重入。
    """
    if os.path.exists(BACKUP):
        if OVERFLOW_TAG in open(INDEX, encoding="utf-8").read():
            shutil.copyfile(BACKUP, INDEX)
            print("  [自愈] 还原上一轮遗留的注入")
        os.remove(BACKUP)


def inject_overflow():
    shutil.copyfile(INDEX, BACKUP)
    src = open(INDEX, encoding="utf-8").read()
    if OVERFLOW_TAG in src:
        return
    src = src.replace('<div id="root"></div>',
                      OVERFLOW_HTML + '<div id="root"></div>')
    open(INDEX, "w", encoding="utf-8").write(src)


def restore():
    if os.path.exists(BACKUP):
        shutil.copyfile(BACKUP, INDEX)
        os.remove(BACKUP)


def run_audit(out, env_extra=None):
    env = dict(os.environ)
    env["LAYOUT_MIN_SCANNED"] = "15"
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, AUDIT, out],
                       cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=560)
    return p.returncode, p.stdout + p.stderr


def anchor_overflow():
    """A：注入超出视口的元素，审计必须报溢出并判未通过。"""
    heal()
    inject_overflow()
    try:
        code, out = run_audit("/tmp/rev_layout_overflow")
    finally:
        restore()
    if code == 0:
        return f"注入溢出元素后审计仍判通过（退出码 0）"
    if "overflow" not in out:
        return "审计判未通过，但汇总里没有 overflow 这一项"
    n = 0
    for line in out.splitlines():
        if line.strip().startswith("overflow:"):
            n = int(line.split(":")[1].split("处")[0].strip())
    if n == 0:
        return "审计判未通过，但 overflow 计数为 0，抓到的不是溢出"
    return None


def anchor_scale():
    """B：阈值抬到不可能满足，必须判盲区而不是四类全零的通过。"""
    heal()
    code, out = run_audit("/tmp/rev_layout_blind",
                          {"LAYOUT_MIN_SCANNED": "999999"})
    if code == 0:
        return "阈值抬高后审计仍判通过：空结果被当成了整洁"
    if "未真正加载" not in out:
        return "审计判未通过，但不是因为规模自检（汇总里没有未真正加载）"
    return None


def anchor_baseline():
    """C：不注入，审计必须通过。"""
    heal()
    code, out = run_audit("/tmp/rev_layout_baseline")
    if code != 0:
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        return "基线未通过：" + " | ".join(tail)
    return None


ANCHORS = {
    "A": ("溢出注入", anchor_overflow),
    "B": ("规模自检", anchor_scale),
    "C": ("基线", anchor_baseline),
}


def main():
    heal()
    want = sys.argv[1] if len(sys.argv) > 1 else "all"
    keys = list(ANCHORS) if want == "all" else [want.upper()]
    bad = []
    for k in keys:
        name, fn = ANCHORS[k]
        try:
            reason = fn()
        except subprocess.TimeoutExpired:
            reason = "审计超时（560 秒）"
        if reason:
            bad.append((name, reason))
            print(f"  [未抓到] {name}：{reason}")
        else:
            print(f"  [抓到] {name}")
    print(f"\n校验点 {len(keys)} 个，精确抓到 {len(keys) - len(bad)} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
