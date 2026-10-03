"""CLI 门禁守卫的判定校验。

## 三个锚点

 * A 撤掉 as_int 上下界 → 4 条变红。同一批输入在 CLI 与 server 被拒、在
   MCP 被放行时，`rounds=0` 得到空转产物却返回成功。
 * B OSError 分支回显异常原文 → 1 条变红（`/dev/null/x`）。
 * C FileNotFoundError 分支回显原文 → 1 条变红（`/proc/nope`）。

## 为什么 B、C 需要专门构造触发场景

收口层有四条分支（UserError / FileNotFoundError / OSError-ValueError-KeyError
/ 兜底 Exception）。若用例全部停在业务层预检，业务层会先给出中文，收口分支
一次都走不到——此时把任一条分支改成回显异常原文，用例照样全绿，四条分支是否
真的收口便无从判断。

`test_data_dir_unusable_is_chinese` 为此把数据目录指到不可用处，让异常真的
冒泡到收口层。两个取值分属两条分支，一条参数化同时压住两条：B 只红
`/dev/null/x`、C 只红 `/proc/nope`，互不替代。

## 取值说明

`/dev/null/x` 恒为 NotADirectoryError（/dev/null 是字符设备）；`/proc/nope`
触发 FileNotFoundError。形如 `某目录/__no_such__/deep` 的取值不适用：代码会
自行建目录并成功，压不到收口层。

点名一律取自实际执行结果，判定用严格模式。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from probes._rev_verdict import pytest_run, verdict  # noqa: E402

TESTS = "tests/test_cli_gate.py"
C = TESTS + "::"
CLI = ROOT / "omegaforge" / "cli.py"
VAL = ROOT / "omegaforge" / "core" / "validate.py"

ANCHORS = [
    ("A 撤掉 as_int 上下界", VAL,
     '        raise UserError(f"{name}需要填写数字，当前格式无法识别")\n'
     "    if minimum is not None and val < minimum:\n"
     '        raise UserError(f"{name}不能小于 {minimum}")\n'
     "    if maximum is not None and val > maximum:\n"
     '        raise UserError(f"{name}不能大于 {maximum}")',
     '        raise UserError(f"{name}需要填写数字，当前格式无法识别")\n'
     "    if False:\n        pass\n    if False:\n        pass",
     [C + "test_distill_numeric_gate",
      C + "test_task_priority_gate"]),
    ("B OSError 分支回显原文", CLI,
     "    except (OSError, ValueError, KeyError) as e:\n"
     "        # JSON 损坏是 ValueError 子类；配置缺键是 KeyError\n"
     "        return _fail(user_error(e))",
     "    except (OSError, ValueError, KeyError) as e:\n"
     "        return _fail(str(e))",
     [C + "test_data_dir_unusable_is_chinese"]),
    ("C 缺文件分支回显原文", CLI,
     "    except FileNotFoundError:\n"
     "        # 不回显路径：完整本机路径属于内部信息，用户只需知道没找到\n"
     '        return _fail("找不到对应的文件或技能，请确认路径/名称是否正确")',
     "    except FileNotFoundError as e:\n        return _fail(f\"{e}\")",
     [C + "test_data_dir_unusable_is_chinese"]),
    ("D 基线", None, None, None, []),
]


def _restore(target: Path, backup: Path):
    shutil.move(str(backup), str(target))


def self_heal():
    """清掉遗留的注入与备份。

    注入若停在源码里，此后每次基线都是红的，而红的原因与当前改动无关
    ——排查方向会被带偏。备份与被注入文件同目录，不放在 /tmp：后者在
    命令之间会被回收，备份没了就无法还原。
    """
    for f in (CLI, VAL):
        bak = Path(str(f) + ".bak")
        if bak.exists():
            _restore(f, bak)
            print(f"  [自愈] 还原 {f.name}")


def main() -> int:
    self_heal()
    bad = []
    for name, target, old, new, expect in ANCHORS:
        if target is None:
            rc, failed, tail = pytest_run(TESTS)
            ok = (rc == 0)
            print(f"  [{'抓到' if ok else '未抓到'}] {name}（{tail}）")
            if not ok:
                bad.append(name)
                print(f"      失败项 {failed[:5]}")
            continue

        src = target.read_text(encoding="utf-8")
        n = src.count(old)
        if n != 1:
            print(f"  [未抓到] {name}：锚点命中 {n} 次，须重定位")
            bad.append(name)
            continue
        bak = Path(str(target) + ".bak")
        shutil.copy2(target, bak)
        try:
            target.write_text(src.replace(old, new, 1), encoding="utf-8")
            if target.read_text(encoding="utf-8") == src:
                print(f"  [未抓到] {name}：写入没生效，注入未落盘")
                bad.append(name)
                continue
            rc, failed, tail = pytest_run(TESTS)
        finally:
            _restore(target, bak)
        ok, why = verdict(rc, failed, expect)
        print(f"  [{'抓到' if ok else '未抓到'}] {name}（{why[:80]}）")
        if not ok:
            bad.append(name)
        rc2, _, tail2 = pytest_run(TESTS)
        if rc2 != 0:
            print(f"      还原后仍非全绿：{tail2}")
            bad.append(name + "（还原失败）")

    print("\n=== 汇总 ===")
    print(f"校验点 {len(ANCHORS)} 个，抓到 {len(ANCHORS) - len(bad)} 个")
    for n in bad:
        print(f"  未抓到：{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
