#!/usr/bin/env python3
"""MCP 协议守卫的判定校验。

MCP 是对外集成面（Claude Desktop 之类客户端直连），协议层失效不会体现为
"应用报错"：客户端只表现为"工具全部失效"或"连上了但没反应"。因此这里的
锚点逐条对到协议硬约束上，而不是只看有没有回包。

## 校验点

 * A 撤掉非 dict 类型闸门 → 一条畸形消息打死整个服务
 * B 撤掉 batch → 数组请求整体无响应
 * C 工具失败改走 JSON-RPC error → 模型看不到原因，无法自我纠正
 * D 缺 method 不判 → 协议级错误码退化
 * E 未知方法不用标准错误码
 * F 参数错误不用标准错误码
 * G 通知也回包 → 客户端收到非预期响应
 * H 协议版本不协商 → 恒回落
 * I 只读工具也标 destructive（防修过头）
 * J 基线

## 备份不放 /tmp

/tmp 在命令之间会被回收，备份没了就无法还原，注入会永久留在源码里。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_selfheal import dirty_names, report_and_stop  # noqa: E402
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_mcp_protocol.py"
C = TESTS + "::"
MCP = ROOT / "omegaforge" / "mcp_server.py"

# A：非 dict 闸门。撤掉后 msg.get 抛 AttributeError，主循环被打断。
A_ON = ('        if not isinstance(msg, dict):\n'
        '            return self._err(None, E_INVALID_REQUEST, '
        '"无效请求：消息必须是一个 JSON 对象")')
A_OFF = ('        if False:\n'
         '            return self._err(None, E_INVALID_REQUEST, '
         '"无效请求：消息必须是一个 JSON 对象")')

# B：batch 逐条处理。
B_ON = ('        responses = [r for r in (self.handle(m) for m in msg) '
        'if r is not None]\n        return responses or None')
B_OFF = ('        responses = []\n        return None')

# C：工具级失败走 result + isError，而不是 JSON-RPC error。
C_ON = ('                    return self._ok(mid, self._tool_error(e, name))')
C_OFF = ('                    return self._err(mid, -32000, str(e))')

# D：缺 method 的协议级判定。
D_ON = ('            if not isinstance(method, str) or not method:\n'
        '                return self._err(mid, E_INVALID_REQUEST, '
        '"无效请求：缺少 method")')
D_OFF = ('            if False:\n'
         '                return self._err(mid, E_INVALID_REQUEST, '
         '"无效请求：缺少 method")')

# E / F：标准错误码。
E_ON = ('                return self._err(mid, E_METHOD_NOT_FOUND,\n'
        '                                 f"不支持的方法：{method}")')
E_OFF = ('                return self._err(mid, -32000,\n'
         '                                 f"不支持的方法：{method}")')

F_ON = ('                if not isinstance(params, dict):\n'
        '                    return self._err(mid, E_INVALID_PARAMS,\n'
        '                                     "调用参数格式不正确，请填写一组参数")')
F_OFF = ('                if not isinstance(params, dict):\n'
         '                    return self._err(mid, -32000,\n'
         '                                     "调用参数格式不正确，请填写一组参数")')

# G：通知不回包。
G_ON = ('            elif method == "notifications/initialized":\n'
        '                return None')
G_OFF = ('            elif method == "notifications/initialized":\n'
         '                result = {}')

# H：版本协商。
H_ON = ('                version = (wanted if wanted in SUPPORTED_PROTOCOL_VERSIONS\n'
        '                           else PROTOCOL_VERSION)')
H_OFF = ('                version = PROTOCOL_VERSION')

# I：只读工具的注解不得标 destructive（防修过头，方向与其它锚点相反）。
I_ON = ('    _READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,\n'
        '                  "idempotentHint": True, "openWorldHint": False}')
I_OFF = ('    _READ_ONLY = {"readOnlyHint": True, "destructiveHint": True,\n'
         '                 "idempotentHint": True, "openWorldHint": False}')

# K：参数别名映射。MCP 的调用方是**别人的模型**，它只看到 schema，只能按语义
#    猜参数名（run_command 大概率传 command，我们内部叫 cmd）。映射撤掉后，
#    每次调用都被"缺少必填参数"拒绝，而模型无从得知正确名字——错误文案里给
#    的正是它没传的那个键，形成死循环。内部前端对不上可以改代码对齐，
#    MCP 对不上是永久失效且改不了客户端。
K_ON = ('        aliases = cls.ARG_ALIASES.get(name) or {}\n'
        '        if not aliases:\n            return args')
K_OFF = ('        aliases = {}\n'
         '        if not aliases:\n            return args')

# L：规范名优先。别名与规范名同时出现时以规范名为准，否则"我们替模型猜一个"。
L_ON = ('        if args:\n'
        '            for canon in set(aliases.values()):\n'
        '                if canon in args:\n'
        '                    out[canon] = args[canon]')
L_OFF = ('        if False:\n'
         '            for canon in set(aliases.values()):\n'
         '                if canon in args:\n'
         '                    out[canon] = args[canon]')

# M：归一化必须先于校验。顺序反了，闸门看到的是别名键，于是判定"缺必填"——
#    别名成了绕过校验之外的另一条死路；更坏的是它表现为"参数不合法"，
#    排查会被带到 schema 上，而不是顺序上。
M_ON = ('        args = self._normalize_args(name, args or {})\n'
        '        self._validate(name, args)')
M_OFF = ('        self._validate(name, args or {})\n'
         '        args = self._normalize_args(name, args or {})')

# N：标注必须叠加进 tools/list 的输出。只填了 TOOL_ANNOTATIONS 表却不接线，
#    外部 host 一个 hint 都收不到，run_command 与 kb_search 在用户面前
#    长得一模一样；而填表这件事本身会让"标注已加"看起来是完成了的。
N_ON = ('            ann = cls.TOOL_ANNOTATIONS.get(spec["name"])\n'
        '            if ann:\n                item["annotations"] = ann')
N_OFF = ('            ann = None\n'
         '            if ann:\n                item["annotations"] = ann')

# O：只读工具标 openWorld（防修过头，方向与"撤掉修复"相反）。
#    host 会据此认为该工具要访问外部世界，确认弹窗与网络策略判定都会走偏。
O_ON = ('    _READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,\n'
        '                  "idempotentHint": True, "openWorldHint": False}')
O_OFF = ('    _READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,\n'
         '                  "idempotentHint": True, "openWorldHint": True}')

ANCHORS = [
    ("A 撤掉非 dict 类型闸门",
     [(MCP, A_ON, A_OFF)],
     [C + "test_non_dict_json_does_not_kill_server",
      C + "test_non_dict_json_returns_invalid_request",
      C + "test_server_still_alive_after_malformed_message"],
     False),
    ("B 撤掉 batch 处理",
     [(MCP, B_ON, B_OFF)],
     [C + "test_batch_processes_each_item"], True),
    ("C 工具失败改走 JSON-RPC error",
     [(MCP, C_ON, C_OFF)],
     [C + "test_tool_error_is_iserror_not_jsonrpc_error",
      C + "test_tool_error_text_is_actionable"], False),
    ("D 缺 method 不判",
     [(MCP, D_ON, D_OFF)],
     [C + "test_missing_method_is_invalid_request"], True),
    ("E 未知方法不用标准错误码",
     [(MCP, E_ON, E_OFF)],
     [C + "test_unknown_method_uses_standard_code"], True),
    ("F 参数错误不用标准错误码",
     [(MCP, F_ON, F_OFF)],
     [C + "test_bad_params_use_standard_code"], True),
    ("G 通知也回包",
     [(MCP, G_ON, G_OFF)],
     [C + "test_notification_returns_none"], True),
    ("H 协议版本不协商",
     [(MCP, H_ON, H_OFF)],
     [C + "test_protocol_version_negotiation"], True),
    ("I 只读工具也标 destructive（防修过头）",
     [(MCP, I_ON, I_OFF)],
     [C + "test_readonly_tools_not_marked_destructive"], True),
    ("K 撤掉参数别名映射",
     [(MCP, K_ON, K_OFF)],
     [C + "test_alias_maps_to_canonical",
      C + "test_alias_works_through_protocol"], False),
    ("L 别名覆盖规范名（不再以规范名为准）",
     [(MCP, L_ON, L_OFF)],
     [C + "test_canonical_wins_over_alias"], True),
    ("M 归一化挪到校验之后",
     [(MCP, M_ON, M_OFF)],
     [C + "test_alias_does_not_bypass_gate"], True),
    ("N 标注不叠加进 tools/list",
     [(MCP, N_ON, N_OFF)],
     [C + "test_every_tool_has_annotations",
      C + "test_annotations_reach_protocol"], False),
    ("O 只读工具标 openWorld（防修过头）",
     [(MCP, O_ON, O_OFF)],
     [C + "test_open_world_only_for_network_and_exec"], True),
    ("J 基线", [], [], True),
]

FILES = [MCP]


def _restore(path: Path, bak: Path):
    shutil.move(str(bak), str(path))


def self_heal() -> bool:
    for path in FILES:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            _restore(path, bak)
            print(f"  [自愈] 还原 {path.name}")
    names = dirty_names(FILES, ROOT)
    if names:
        return report_and_stop(names)
    return True


def main() -> int:
    if not self_heal():
        return 1
    want = sys.argv[1:]
    bad = []
    for name, edits, expect, strict in ANCHORS:
        if want and name[0] not in want:
            continue
        if not edits:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        paths = []
        missing = False
        for path, old, _new in edits:
            src = path.read_text(encoding="utf-8")
            if src.count(old) != 1:
                print(f"  [未抓到] {name}：锚点命中 {src.count(old)} 次，"
                      f"须重定位（{path.name}）")
                bad.append(name)
                missing = True
                break
            if path not in paths:
                paths.append(path)
        if missing:
            continue

        baks = []
        try:
            for path in paths:
                bak = Path(str(path) + ".bak")
                shutil.copy2(path, bak)
                baks.append((path, bak))
            for path, old, new in edits:
                src = path.read_text(encoding="utf-8")
                path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed, tail = pytest_run(TESTS)
        finally:
            for path, bak in baks:
                if bak.exists():
                    _restore(path, bak)
        ok, why = verdict(rc, failed, expect, strict=strict)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:56]}）")
        if not ok:
            bad.append(name)
            print(f"      失败项 {failed[:6]}")
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    ran = len([a for a in ANCHORS if not want or a[0][0] in want])
    print(f"本段校验点 {ran} 个，抓到 {ran - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
