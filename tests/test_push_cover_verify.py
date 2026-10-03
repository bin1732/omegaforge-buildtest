#!/usr/bin/env python3
"""覆盖推送的完整性校验。

推送环节最容易蒙混过关的地方是**只比数量**：文件条数吻合看起来像内容
吻合，但上传时若把内容换掉（例如按文本而非二进制提交，或做了编码），
条数分毫不差，只有 blob sha 会暴露——sha 由内容决定，逐条相等即内容
逐条相等。

本用例覆盖两件事：

1. 比对函数本身对"缺失 / 多余 / 内容不符 / 全等"四种情形的判定；
2. 推送脚本在导入时不依赖凭据文件——否则用例一加载就崩，比对逻辑无从
   被单独检验，而这类崩溃又常被读成"网络或凭据配置问题"。
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load():
    # 仓库根目录按本文件位置推，不认脚本里的默认值：脚本默认指向沙盒内的
    # 固定路径，换一台机器（例如 CI 的 Windows runner）该路径不存在，
    # 于是取树失败，症状是 git 报 128，看着像仓库坏了。
    os.environ["REPO_ROOT"] = str(ROOT)
    spec = importlib.util.spec_from_file_location(
        "push_force_cover", ROOT / "scripts" / "push_force_cover.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTreeDiff:
    """比对函数对四种情形的判定。"""

    def test_all_equal(self):
        m = load()
        assert m.diff_trees({"a": "1", "b": "2"},
                            {"a": "1", "b": "2"}) == ([], [], [])

    def test_remote_missing(self):
        m = load()
        missing, extra, bad = m.diff_trees({"a": "1", "b": "2"}, {"a": "1"})
        assert missing == ["b"] and extra == [] and bad == []

    def test_remote_extra(self):
        m = load()
        missing, extra, bad = m.diff_trees(
            {"a": "1"}, {"a": "1", "z": "9"})
        assert missing == [] and extra == ["z"] and bad == []

    def test_content_mismatch_keeps_count(self):
        """内容被换掉时条数不变——只比数量会判为一致。"""
        m = load()
        local = {"a": "1", "b": "2"}
        remote = {"a": "1", "b": "ff" * 20}
        assert len(local) == len(remote)
        missing, extra, bad = m.diff_trees(local, remote)
        assert (missing, extra, bad) == ([], [], ["b"])

    def test_several_mismatches_are_all_reported(self):
        m = load()
        local = {k: str(i) for i, k in enumerate("abcde")}
        remote = dict(local, b="x", d="y")
        _, _, bad = m.diff_trees(local, remote)
        assert bad == ["b", "d"]


class TestModuleIsImportable:
    """推送脚本必须能在没有凭据文件的情况下被加载。"""

    def test_import_without_token_file(self):
        # 指向一个不存在的凭据路径：若模块顶层就读取令牌，导入在此崩溃。
        os.environ["GITHUB_TOKEN_FILE"] = "/nonexistent/token"
        try:
            m = load()
        finally:
            os.environ.pop("GITHUB_TOKEN_FILE", None)
        assert callable(m.diff_trees)

    def test_import_without_target_repo(self):
        """未指定目标仓库时导入不得退出——拒绝只能在入口做。

        导入期 sys.exit 会让整组用例以 SystemExit 失败，报出来的是一批
        "未指定 PUSH_REPO"，看着像环境没配目标仓库，实际是本模块把自己
        的调用方挡在了门外；而用例正是校验它自身行为的手段。
        """
        saved = os.environ.pop("PUSH_REPO", None)
        try:
            m = load()
        finally:
            if saved is not None:
                os.environ["PUSH_REPO"] = saved
        assert callable(m.diff_trees)

    def test_token_is_read_lazily(self):
        """令牌只在真正发起请求时才读。"""
        src = (ROOT / "scripts" / "push_force_cover.py").read_text(
            encoding="utf-8")
        head = src.split("def api(")[0]
        assert "open(" not in head or "def token(" in head


class TestLocalTree:
    def test_local_tree_matches_tracked_files(self):
        m = load()
        tree = m.local_tree()
        tracked = [f for f in subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(ROOT),
            capture_output=True, text=True).stdout.split("\0") if f]
        # 本地树的条目应覆盖全部纳入版本控制的文件；两者数量一致时，
        # "逐条比对"才有意义（否则比对的是两份不同的清单）。
        assert set(tracked) <= set(tree), (
            f"有文件不在 HEAD 树里：{sorted(set(tracked) - set(tree))[:5]}")
        assert tree, "本地树为空，比对将恒等通过"


class TestTruncatedTreeMustNotDriveDeletion:
    """远端清单被接口截断时，绝不能用它算删除项。

    递归取树的接口在条目过多时返回 truncated=true，tree 只是**一部分**。
    拿这份不完整清单当基底，"接口没返回的文件"会被读成"本地已删除的文件"，
    进而在增量推送里触发批量删除——远端整片文件消失，而脚本只报一句
    "需删除 N 个"，看不出是误删。因此不完整时必须退化为全量。
    """

    def test_partial_list_yields_no_deletions(self):
        m = load()
        files = ["a.py", "b.py", "c.py"]
        local = {"a.py": "1", "b.py": "2", "c.py": "3"}
        # 接口只返回了 a.py：若据此算删除，b/c 会被当成"本地已删除"
        partial = {"a.py": "1"}
        todo, gone, use_base = m.plan_push(files, local, partial,
                                           complete=False)
        assert gone == [], f"截断清单不得产生删除项：{gone}"
        assert use_base is False, "截断清单不得作为基底"
        assert sorted(todo) == files, "应退化为全量重传全部文件"

    def test_complete_list_still_deletes_real_removals(self):
        """清单完整时，本地真删掉的文件必须出现在删除项里。"""
        m = load()
        files = ["a.py", "b.py"]
        local = {"a.py": "1", "b.py": "2"}
        remote = {"a.py": "1", "b.py": "2", "old.py": "9"}
        todo, gone, use_base = m.plan_push(files, local, remote,
                                           complete=True)
        assert use_base is True
        assert gone == ["old.py"]

    def test_empty_remote_reuploads_everything(self):
        """远端为空（新分支）时全量重传，且不得产生删除项。

        与"清单被截断"的区别是：这里确实没有文件，重传全部是正确动作；
        截断时重传全部则是为了避免误删，两者 todo 相同但理由不同。
        """
        m = load()
        files = ["a.py", "b.py"]
        local = {"a.py": "1", "b.py": "2"}
        todo, gone, use_base = m.plan_push(files, local, {}, complete=True)
        assert sorted(todo) == files and gone == []
        assert use_base is False, "空基底不必以远端树为基底，重传即可"


class TestFetchTreeReportsTruncation:
    """取树的结果必须带上"是否完整"这个标记。

    只返回映射、不带标记的话，调用方无从区分"远端确实只有这些文件"与
    "接口只给了这些"，两种情形在数据结构上完全一样，差别只在这个标记里。
    """

    def _stub(self, m, monkeypatch, payload):
        monkeypatch.setattr(m, "api", lambda *a, **k: payload)

    def test_truncated_flag_is_propagated(self, monkeypatch):
        m = load()
        self._stub(m, monkeypatch, {
            "tree": [{"path": "a.py", "type": "blob", "sha": "1"}],
            "truncated": True,
        })
        entries, complete = m.fetch_tree("deadbeef")
        assert entries == {"a.py": "1"}
        assert complete is False, "truncated=true 必须被报为不完整"

    def test_complete_tree_is_reported_complete(self, monkeypatch):
        m = load()
        self._stub(m, monkeypatch, {
            "tree": [{"path": "a.py", "type": "blob", "sha": "1"}],
            "truncated": False,
        })
        entries, complete = m.fetch_tree("deadbeef")
        assert entries == {"a.py": "1"} and complete is True

    def test_absent_flag_counts_as_complete(self, monkeypatch):
        """字段缺失时按完整处理：接口只在截断时才带这个字段。"""
        m = load()
        self._stub(m, monkeypatch, {
            "tree": [{"path": "a.py", "type": "blob", "sha": "1"}],
        })
        _, complete = m.fetch_tree("deadbeef")
        assert complete is True

    def test_remote_tree_propagates_incomplete(self, monkeypatch):
        m = load()

        def fake(method, path, payload=None, raw=False):
            if path.endswith("/git/ref/heads/master"):
                return {"object": {"sha": "c" * 40}}
            if path.endswith("/git/commits/" + "c" * 40):
                return {"tree": {"sha": "t" * 40}}
            if path.startswith("/git/trees/"):
                return {"tree": [], "truncated": True}
            raise AssertionError(path)

        monkeypatch.setattr(m, "api", fake)
        sha, entries, complete = m.remote_tree("master")
        assert sha == "t" * 40 and complete is False


class TestRefUpdateCreatesWhenMissing:
    """引用不存在时必须转为创建，而不是以失败收场。

    对不存在的引用做 PATCH 返回 422 "Reference does not exist"——此时文件
    已全部上传完毕，失败发生在最后一步，整轮上传白做。而这条消息读起来像
    "分支名写错了"，并不说"这个分支还没有"，排查方向会被带偏。
    """

    def test_patch_success_does_not_create(self, monkeypatch):
        m = load()
        calls = []

        def fake(method, path, payload=None, raw=False):
            calls.append((method, path))
            return {}

        monkeypatch.setattr(m, "api", fake)
        m.update_ref("main", "a" * 40)
        assert calls == [("PATCH", "/git/refs/heads/main")]

    def test_422_falls_back_to_create(self, monkeypatch):
        m = load()
        calls = []

        def fake(method, path, payload=None, raw=False):
            calls.append((method, path, payload))
            if method == "PATCH":
                raise SystemExit("PATCH /git/refs/heads/main 失败 422: "
                                 "Reference does not exist")
            return {}

        monkeypatch.setattr(m, "api", fake)
        m.update_ref("main", "b" * 40)
        assert calls[0][0] == "PATCH"
        assert calls[1][0] == "POST" and calls[1][1] == "/git/refs"
        assert calls[1][2] == {"ref": "refs/heads/main", "sha": "b" * 40}

    def test_other_failures_are_not_swallowed(self, monkeypatch):
        """只有 422 才转为创建：别的失败必须照原样抛出。"""
        m = load()

        def fake(method, path, payload=None, raw=False):
            raise SystemExit("PATCH /git/refs/heads/main 失败 403: forbidden")

        monkeypatch.setattr(m, "api", fake)
        try:
            m.update_ref("main", "c" * 40)
        except SystemExit as exc:
            assert "403" in str(exc)
        else:
            raise AssertionError("非 422 的失败不得被吞掉")


class TestBranchResolution:
    """未显式指定分支时取仓库的默认分支。"""

    def test_explicit_branch_wins(self, monkeypatch):
        m = load()
        monkeypatch.setenv("PUSH_BRANCH", "somewhere")
        monkeypatch.setattr(m, "api", lambda *a, **k: {"default_branch": "main"})
        assert m.resolve_branch() == "somewhere"

    def test_defaults_to_repo_default_branch(self, monkeypatch):
        m = load()
        monkeypatch.delenv("PUSH_BRANCH", raising=False)
        monkeypatch.setattr(m, "api", lambda *a, **k: {"default_branch": "main"})
        assert m.resolve_branch() == "main"
