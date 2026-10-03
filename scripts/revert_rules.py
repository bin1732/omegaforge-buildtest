#!/usr/bin/env python3
"""规则层守卫的回退校验：撤掉修复，指定的用例必须变红。

## 为什么要点名到用例

只比对"有没有失败项"区分不了两件事：失败的是不是这一处守卫对应的那条
用例。给被测模块加一个模块级未定义名，整片用例都会失败，任何"有失败"
的判定都会被满足——那时的红是环境故障，不是守卫在起作用。

因此每个校验点都点名预期变红的用例，并且严格：出现预期之外的失败项同样
判未抓到。

## 校验点的分工

critical 清单那一族（A~E）与规则存储那一族（F~L）注入的是同一个文件的
不同位置，互相证不了对方：前者管"这条命令该不该拦"，后者管"记住的规则
在什么条件下还算数"。只留一族，另一整族失效不会有任何校验点变红。

用法：

    python3 scripts/revert_rules.py            # 全部校验点
    python3 scripts/revert_rules.py F G        # 只跑指定校验点
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT", "/data/workspace/LATEST"))
sys.path.insert(0, str(ROOT / "probes"))

from _rev_verdict import pytest_run, restore_src, verdict  # noqa: E402

SRC = ROOT / "omegaforge" / "tools" / "rules.py"
TESTS = "tests/test_rules_contract.py"
BACKUP = Path("/data/workspace/.rev_backups/rules.py.bak")


def case(name: str) -> str:
    return f"{TESTS}::{name}"


# (标识, 说明, 注入前, 注入后, 预期变红的用例)
ANCHORS: list[tuple[str, str, str, str, list[str]]] = [
    # ---------------- critical 清单那一族 ----------------
    ("A", "critical 匹配前先归一化（否则变形写法绕过）",
     '    cmd = normalize(str(cmd or ""))',
     '    cmd = str(cmd or "")',
     [case("test_cmd_critical_survives_obfuscation"),
      case("test_cmd_critical_normalizes_before_matching")]),
    ("B", "配置注入类命令（core.pager 等）",
     '"core.pager", "core.editor"',
     '"__disabled.core.pager", "core.editor"',
     [case("test_config_injection_blocked")]),
    ("C", "alias + ! 组合",
     'if "alias." in low and "!" in cmd:',
     'if False and "alias." in low and "!" in cmd:',
     [case("test_config_injection_blocked")]),
    ("D", "fs 选择器按归一化形式匹配",
     'return {"path": "/".join(_path_parts(normalize(args.get("rel", ""))))}',
     'return {"path": "/".join(_path_parts(args.get("rel", "")))}',
     [case("test_selectors_normalized")]),
    ("D2", "terminal 选择器按归一化形式匹配",
     '        return {"prog": program_of(normalize(args.get("cmd", "")))}',
     '        return {"prog": program_of(args.get("cmd", ""))}',
     [case("test_terminal_selector_normalized")]),
    ("E", "list(include_expired) 真的透传",
     '        return self._load(include_expired=include_expired)',
     '        return self._load()',
     [case("test_list_include_expired_actually_works"),
      case("test_expired_session_rule_visible_for_audit"),
      case("test_legacy_session_rule_without_session_id_is_expired")]),

    # ---------------- 规则存储那一族 ----------------
    ("F", "session 规则绑定会话标识",
     '        if not sid or sid != session_id:',
     '        if False:',
     [case("test_session_rule_does_not_survive_new_session"),
      case("test_expired_session_rule_visible_for_audit"),
      case("test_legacy_session_rule_without_session_id_is_expired")]),
    ("G", "缺 session_id 的老规则判失效（不兼容保留）",
     '        if not sid or sid != session_id:',
     '        if sid and sid != session_id:',
     [case("test_legacy_session_rule_without_session_id_is_expired")]),
    ("H", "脏时间戳不得拖垮解析",
     '    if isinstance(v, bool) or not isinstance(v, (int, float)):\n'
     '        return None',
     '    pass',
     [case("test_coerce_ts_rejects_non_numeric")]),
    ("I", "逐条容错：单条脏记录不得拖垮整条规则链",
     '            try:\n'
     '                gone = _rule_expired(r, now, self.session_id)\n'
     '            except Exception:\n'
     '                gone = True',
     '            gone = _rule_expired(r, now, self.session_id)',
     [case("test_load_survives_rule_expired_failure")]),
    ("J", "revoke 按全集取（脏/过期规则也能精确删）",
     '        rules = self._load(include_expired=True)\n'
     '        left = [r for r in rules if r.get("id") != rule_id]',
     '        rules = self._load()\n'
     '        left = [r for r in rules if r.get("id") != rule_id]',
     [case("test_revoke_can_remove_dirty_rule")]),
    ("K", "purge 不依赖能否解析（损坏时是唯一自救通道）",
     '        try:\n'
     '            n = len(self._load(include_expired=True))\n'
     '        except Exception:\n'
     '            n = 0',
     '        n = len(self._load(include_expired=True))',
     [case("test_purge_works_even_when_load_raises")]),
    ("L", "deny 裁决权重最高（低层级 allow 不得覆盖）",
     'DECISION_RANK = {"deny": 3, "ask": 2, "allow": 1}',
     'DECISION_RANK = {"deny": 1, "ask": 2, "allow": 3}',
     [case("test_managed_deny_not_overridable_by_session_allow")]),
]


def _git_restore() -> None:
    """用版本库恢复源码。

    备份被移走、内容对不上、或中断留下的污染无法就地判别时，版本库是
    唯一可信的干净版本。没有这道兜底，一次中断就会让源码长期停在注入
    态——而后续所有校验点都在脏代码上跑，结论全部失真。
    """
    os.system(f"cd {ROOT} && git checkout -- {SRC.relative_to(ROOT)}")


def main() -> int:
    keys = sys.argv[1:] or [a[0] for a in ANCHORS]
    wanted = [a for a in ANCHORS if a[0] in keys]
    unknown = [k for k in keys if k not in {a[0] for a in ANCHORS}]
    if unknown:
        print(f"未知校验点：{unknown}")
        return 1

    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    # 开局自愈：注入残留与干净源码在文件上都是一段合法代码，靠内容区分
    # 不了。故先与版本库对齐，再读取基准内容。
    import subprocess as _sp
    _diff = _sp.run(["git", "diff", "--quiet", "--",
                     str(SRC.relative_to(ROOT))], cwd=str(ROOT))
    if _diff.returncode != 0:
        print("源码与版本库不一致，先恢复（上一轮可能中断在注入态）")
        _git_restore()
    original = SRC.read_text(encoding="utf-8")

    ok = True
    results = []
    try:
        for key, label, old, new, expect in wanted:
            # 备份必须在每次注入之前重写：还原动作是 move，备份文件会被
            # 移走，下一个校验点就找不到它——于是还原静默失败，源码停在
            # 已注入状态，而后续所有校验点都在脏代码上跑。
            BACKUP.write_text(original, encoding="utf-8")
            if original.count(old) != 1:
                print(f"[{key}] 锚点不唯一（命中 {original.count(old)} 次）"
                      f"——无法确定改的是哪一处")
                results.append((key, label, False, "锚点不唯一"))
                ok = False
                continue
            patched = original.replace(old, new, 1)
            try:
                ast.parse(patched)
            except SyntaxError as e:
                print(f"[{key}] 注入后语法错误 {e}——结果不可信")
                results.append((key, label, False, "注入后语法错误"))
                ok = False
                continue
            # 注入必须落在 try 之内：写在 try 之外的话，还原一旦抛异常，
            # 注入就永久留在源码里，而它看起来只是"这个校验点没跑完"。
            try:
                SRC.write_text(patched, encoding="utf-8")
                rc, failed, _tail = pytest_run(TESTS)
            finally:
                if BACKUP.exists():
                    restore_src(SRC, BACKUP)
                if SRC.read_text(encoding="utf-8") != original:
                    print(f"[{key}] 还原不一致，改用版本库恢复")
                    _git_restore()
            got, msg = verdict(rc, failed, expect)
            print(f"[{key}] {label}: {'抓到' if got else '未抓到'} | {msg}")
            results.append((key, label, got, msg))
            ok = ok and got
    finally:
        if SRC.read_text(encoding="utf-8") != original:
            if BACKUP.exists():
                restore_src(SRC, BACKUP)
            if SRC.read_text(encoding="utf-8") != original:
                _git_restore()

    print("-" * 68)
    rc, failed, tail = pytest_run(TESTS)
    print(f"  基线：{tail}")
    if rc != 0:
        print("  基线不干净——校验结论不可采信")
        ok = False
    print(f"\n{sum(1 for r in results if r[2])}/{len(results)} 抓到")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
