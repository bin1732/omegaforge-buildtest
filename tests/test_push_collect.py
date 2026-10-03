# -*- coding: utf-8 -*-
"""推送收集口径守卫：只推版本库已跟踪的文件。

## 验的是什么

推送走 API，收集阶段决定"什么算源码"。若收集用目录遍历，任何本地
新增目录都会被当成源码推上去——一次依赖安装就能让条目数翻倍，而这些
文件既不在 .gitignore 里也不在排除表里。

这类污染有两个坏处：

  1. 远端仓库混进与产品无关的依赖树，"仓库里有什么"不再可信；
  2. 推送耗时成倍增长，最后在某个无关文件上读取失败，报错看着像
     "仓库坏了"，排查方向被带到别处。

## 判定

  * 收集结果不得包含未跟踪目录里的文件（活体断言：真造一个未跟踪目录）
  * 收集结果必须非空，且条目数在合理量级内（空结果会让后续推送静默
    变成"把远端清空"）
  * 已跟踪文件若本地缺失必须判失败，不得静默跳过
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """按文件路径加载推送脚本（它不是可导入模块：顶层就要读令牌）。"""
    os.environ.setdefault("PUSH_REPO", "bin1732/omegaforge-buildtest")
    spec = importlib.util.spec_from_file_location(
        "push_repo_under_test", ROOT / "scripts" / "push_repo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_untracked_dir_never_collected():
    """真造一个未跟踪目录：它不得出现在收集结果里。"""
    mod = _load()
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        junk_dir = Path(td)
        # 造一个足够像依赖树的目录：有包结构、有多个文件
        (junk_dir / "pkg").mkdir()
        for i in range(5):
            (junk_dir / "pkg" / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
        rel_prefix = junk_dir.relative_to(ROOT).as_posix()
        files = mod.collect()
        leaked = [f for f in files if f.startswith(rel_prefix)]
        assert not leaked, (
            f"未跟踪目录被当成源码收集（{len(leaked)} 条，例：{leaked[:3]}）——"
            "收集阶段必须只取版本库已跟踪的文件")


def test_collect_is_nonempty_and_bounded():
    """空结果会让推送静默变成清空远端；条目数失控说明收集口径又漂了。"""
    mod = _load()
    files = mod.collect()
    assert len(files) >= 100, (
        f"收集结果只有 {len(files)} 条，疑似口径失效——空结果会把远端清空")
    assert len(files) <= 2000, (
        f"收集结果 {len(files)} 条，远超源码应有的量级——"
        "多半有本地目录被当成源码收进来了")


def test_collector_uses_git_tracking():
    """口径本身用 git ls-files：退回目录遍历会重新引入污染。

    这条不替代上面两条活体断言，而是让"改成遍历"这个动作当场失败，
    并指出为什么不能改。
    """
    mod = _load()
    # 只取 collect 自身的源码：全文件里搜 "os.walk" 会把 .bak 扫描那处
    # 合法用法也判成违规——守卫报了错，指的地方却不是问题所在。
    import inspect
    src = inspect.getsource(mod.collect)
    assert "git" in src and "ls-files" in src, (
        "推送收集须以 git ls-files 为准；目录遍历会把本地未跟踪目录当成源码")
    assert "os.walk" not in src, (
        "收集函数不得使用 os.walk —— 未跟踪目录会随之进入远端")


def test_default_branch_not_hardcoded():
    """目标分支取远端默认分支。

    写死 master 而远端默认分支是 main 时，PATCH 打在不存在（或已删掉）的
    引用上：整轮几十分钟的 blob 上传全部作废，而报错看着像"引用不合法"，
    排查方向被带到仓库权限上。
    """
    mod2 = _load()
    src = inspect.getsource(mod2.main)
    assert "master" not in src, (
        "推送目标分支不得写死 master —— 与远端默认分支不符时整轮上传作废")
    assert "_default_branch" in src, "须取远端默认分支；不存在时转为创建"


def test_commit_message_not_hardcoded():
    """提交说明须取本地 HEAD。

    写死一句的话，远端每条提交都记着同一句与实际改动无关的话，事后从提交
    历史看不出这批改了什么——排查 CI 失败时第一件事就是看"这批动了什么"。
    """
    mod2 = _load()
    src = inspect.getsource(mod2.main)
    assert "OmegaForge 全维度重构" not in src, "提交说明仍写死为与改动无关的一句"
    assert "_commit_message" in src, "提交说明须取自本地 HEAD 的提交说明"


def test_no_backup_file_is_tracked():
    """.bak 被提交进版本库后，推送会跟着把它推走，自愈还会拿它当干净版本。

    曾发生过：一次 `git add -A` 把注入残留的 .bak 收进版本库，于是远端
    源码里混着一份改坏的副本，而自愈按"备份=干净版本"还原，源码永远停在
    注入态并一路报"已还原"。
    """
    mod = _load()
    leaked = [f for f in mod.collect() if f.endswith(".bak")]
    assert not leaked, (
        f"注入备份被当成源码收集（{len(leaked)} 条，例：{leaked[:3]}）——"
        ".bak 进版本库后会被推送，且自愈会误把它当作干净版本还原")


def test_no_hardcoded_target_branch():
    """目标分支写死 master 时，整轮上传会打在不存在的引用上全部作废。"""
    src = (ROOT / "scripts" / "push_repo.py").read_text(encoding="utf-8")
    assert 'refs/heads/master"' not in src, (
        "推送目标分支不得写死 master —— 远端默认分支是 main 时，"
        "十几分钟的 blob 上传会全部作废，报错看着像引用不合法")


def test_commit_message_comes_from_head():
    """提交说明写死时，远端每条提交记着同一句，事后看不出这批改了什么。"""
    src = (ROOT / "scripts" / "push_repo.py").read_text(encoding="utf-8")
    assert "_commit_message()" in src, (
        "提交说明须取本地 HEAD；_commit_message() 已定义却从未被调用的话，"
        "这个修复等于没接进去")
    assert "全维度重构（第一批）" not in src, (
        "提交说明不得写死——写死后从提交历史看不出这批改了什么")


def _sha_mod():
    import importlib.util, os
    os.environ.setdefault("PUSH_REPO", "bin1732/omegaforge-buildtest")
    spec = importlib.util.spec_from_file_location(
        "pr_sha", ROOT / "scripts" / "push_repo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_blob_sha_matches_git():
    """复用远端 blob 的前提是算出的 sha 与 git 完全一致。

    不一致时每个文件都判为"需要上传"，复用静默失效——症状只是推送又慢回
    原样，看不出是 sha 算错了。直接跟 git hash-object 对账。
    """
    mod = _sha_mod()
    rel = "README.md" if (ROOT / "README.md").is_file() else next(
        ROOT.glob("*.md")).name
    want = subprocess.run(["git", "hash-object", rel], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    assert want, "git hash-object 取不到值，无法对账"
    assert mod._git_blob_shas([rel]).get(rel) == want, (
        f"blob sha 与 git 不一致（{want[:10]}）——复用判定会全部落空，"
        "推送慢回原样且不报错")


def test_blob_sha_uses_git_not_handrolled():
    """sha 必须由 git 自己算，不能是手工 sha1。

    手工 sha1 在没有 clean 过滤器时与 git 一致，于是本地恒真；而 Windows
    上 core.autocrlf 默认 true，检出 CRLF、git 存 LF，手工值对不上——复用
    静默全部落空。故按源码判定：必须委托给 git hash-object。
    """
    src = (ROOT / "scripts" / "push_repo.py").read_text(encoding="utf-8")
    assert "hash-object" in src, (
        "blob sha 须由 git hash-object 计算；手工 sha1 在启用行尾规范化的"
        "平台上会静默失配")
    body = inspect.getsource(_sha_mod().main)
    assert "shas[rel]" in body, (
        "复用判定须用 git 算出的 sha（shas[rel]）；改用手工值会让复用在"
        "启用行尾规范化的平台上静默全部落空")
    assert "_blob_sha(data)" not in body, (
        "主流程不得用手工 sha1 参与复用判定")


def test_blob_sha_is_filter_aware():
    """启用行尾规范化时，取值必须仍等于 git 的值。

    在临时仓库里打开 autocrlf 并写入 CRLF：磁盘字节与 git 存储字节不同，
    手工 sha1 必然算错。这条让失配在任何平台上都能被抓到，而不必等到换了
    操作系统才暴露。
    """
    mod = _sha_mod()
    d = tempfile.mkdtemp(prefix="of_sha_")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=d,
                   check=True)
    (Path(d) / "a.txt").write_bytes(b"line1\r\nline2\r\n")
    want = subprocess.run(["git", "hash-object", "a.txt"], cwd=d,
                          capture_output=True, text=True).stdout.strip()
    raw = (Path(d) / "a.txt").read_bytes()
    assert mod._blob_sha(raw) != want, (
        "前置不成立：此环境下手工 sha1 与 git 相同，这条用例验不到东西")
    orig = mod.ROOT
    try:
        mod.ROOT = Path(d)
        got = mod._git_blob_shas(["a.txt"]).get("a.txt")
    finally:
        mod.ROOT = orig
    assert got == want, (
        f"启用行尾规范化后取值与 git 不符（git={want[:10]} got={str(got)[:10]}）"
        "——复用判定会静默全部落空")


def test_missing_tracked_file_is_fatal():
    """已跟踪文件本地缺失时须判失败：静默跳过会让远端少文件而本地看不出来。"""
    src = (ROOT / "scripts" / "push_repo.py").read_text(encoding="utf-8")
    assert "is_file()" in src, (
        "收集须校验已跟踪文件在本地存在；缺失时静默跳过会造成远端文件丢失")
    assert "已跟踪文件在本地缺失" in src, "缺失须显式判失败，不得只跳过"
