"""说明文字整洁度扫描器的覆盖面。

扫描报"通过"只代表**被扫到的部分**整洁。若某个目录不在扫描范围内，
那里的作业记录一条都查不到，而汇总依然显示通过——检查看上去在起作用，
实际对那一整片目录是失效的。

本守卫验的是覆盖面本身：在三个目录各放一份带问题的样本，扫描必须
都报出来；再把目录从范围里去掉，同类问题必须查不到。

样本写在变量里而非注释里，避免本文件自己命中整洁度规则。
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "scripts" / "check_comment_hygiene.py"

SAMPLE_DIRTY = "# 上一轮这里会报错"
SAMPLE_CHAPTER = "# 与第 33/38/40 章一致"


def _load():
    sys.argv = ["x"]
    spec = importlib.util.spec_from_file_location(
        "ch_scope_%d" % os.getpid(), str(SCANNER))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def test_scan_covers_probes_and_scripts():
    mod = _load()
    base, _ = mod.scan(ROOT)
    assert base == [], f"基线应整洁，实际命中 {len(base)} 处：{base[:3]}"

    planted = {
        ROOT / "probes" / "_scope_sample_a.py": SAMPLE_DIRTY,
        ROOT / "scripts" / "_scope_sample_b.py": SAMPLE_DIRTY,
        ROOT / "omegaforge" / "_scope_sample_c.py": SAMPLE_CHAPTER,
    }
    try:
        for path, text in planted.items():
            path.write_text('"""覆盖面样本。"""\n' + text + "\nX = 1\n",
                            encoding="utf-8")
            fails, _ = mod.scan(ROOT)
            hit = [f for f in fails if path.name in f[1]]
            assert hit, f"{path.parent.name}/ 里的说明文字未被扫描到（目录盲区）"
    finally:
        for path in planted:
            if path.exists():
                path.unlink()


def test_scope_removal_is_detected():
    """把两个目录从范围里去掉后，同类问题必须查不到。

    这条是防"扫描范围被改回去而没人发现"：只看命中数无法区分
    "全仓整洁"与"那片目录根本没扫"。
    """
    mod = _load()
    src = SCANNER.read_text(encoding="utf-8")
    anchor = '    for extra in ("probes", "scripts"):'
    assert anchor in src, "范围锚点已变化，本守卫需同步"
    sample = ROOT / "probes" / "_scope_sample_d.py"
    try:
        sample.write_text('"""覆盖面样本。"""\n' + SAMPLE_DIRTY + "\nX = 1\n",
                          encoding="utf-8")
        SCANNER.write_text(src.replace(anchor, "    for extra in ():"),
                           encoding="utf-8")
        mod2 = _load()
        fails, _ = mod2.scan(ROOT)
        hit = [f for f in fails if sample.name in f[1]]
        assert not hit, "去掉范围后仍然命中，说明覆盖面校验本身失效"
    finally:
        SCANNER.write_text(src, encoding="utf-8")
        if sample.exists():
            sample.unlink()


def test_classification_is_platform_independent():
    """目录分类不得随平台分隔符变化。

    相对路径的字符串形态随平台变化：Windows 上为反斜杠。若前缀判定改用
    os.sep 拼接，那段代码只在 Windows 上生效——三个目录会整体改用更严的
    源码词表，而本地无论怎么跑都复现不了，症状表现为"只有 CI 报红"。

    因此要求：路径归一为正斜杠，前缀判定用字面量，不读 os.sep。
    """
    src = SCANNER.read_text(encoding="utf-8")
    # 只查代码：注释里解释"为什么不用 os.sep"是正当说明，按整段文本匹配
    # 会把说明本身判成违规——与整洁度扫描器自己的口径保持一致。
    code = "\n".join(
        t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
        if t.type not in (tokenize.COMMENT,))
    assert "os.sep" not in code, (
        "扫描器不应按 os.sep 判定目录：该分支只在非本地平台生效")
    assert ".as_posix()" in src, "相对路径必须归一为正斜杠形态"

    mod = _load()
    base, _ = mod.scan(ROOT)
    import os
    saved = os.sep
    try:
        os.sep = "\\"
        after, _ = mod.scan(ROOT)
    finally:
        os.sep = saved
    assert after == base, (
        "分隔符切换后结果发生变化，分类仍受平台影响")
