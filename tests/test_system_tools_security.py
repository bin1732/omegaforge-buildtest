"""系统工具安全回归测试（pytest，CI 会真实执行）。

本文件覆盖的三类缺陷都是**可复现**出来的，不是推测：

 1. 危险命令黑名单失效
   原正则 `\\b(sudo|...|rm\\s+-rf\\s+/|...|:(){ :|& };;)\\b` 有两个致命问题：
    a. 以非单词字符结尾的分支（"rm -rf /"）尾部 \\b 在行尾不成立
     → 验证 `rm -rf /` **不被拦截**（最危险的命令反而漏掉）
    b. fork 炸弹含 ()|&{} 正则元字符，被当成语法解析
     → 验证 `:(){ :|:& };:` **不被拦截**

 2. 工作区路径前缀校验可被绕过
   原实现 `str(p).startswith(str(home))`
   → 同级目录 ".omegaforge_evil" 同样以 ".omegaforge" 开头，
    验证可用 "../.omegaforge_evil/secret.txt" 越权读取。

 3. web_fetch 无内网地址防护（SSRF）
   → 验证可请求 127.0.0.1 / 169.254.169.254（云元数据）。

每条都同时验证"该拦的拦住"与"不该拦的放行"，防止为过测试而一刀切。
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.tools.system_tools import ( # noqa: E402
  SystemTools,
  _dangerous,
  _guard_public_url,
)


# ---------------------------------------------------------------- 危险命令

@pytest.mark.parametrize("cmd", [
  "rm -rf /",
  "rm -rf  /",     # 多空格归一化后仍须识别
  "rm -rf /tmp",
  "sudo ls",
  "su -",
  "mkfs.ext4 /dev/sda",
  ":(){ :|:& };:",    # fork 炸弹：字面匹配，不能进正则
  "shutdown -h now",
  "reboot",
  "poweroff",
  "dd if=/dev/zero of=/dev/sda",
])
def test_dangerous_commands_blocked(cmd):
  assert _dangerous(cmd) is True, f"危险命令未被拦截: {cmd!r}"


@pytest.mark.parametrize("cmd", [
  "ls -la",
  "cat README.md",
  "python3 -m pytest -q",
  "echo hello",
  "git status",
  "grep -r omegaforge .",
  "npm run build",
  "pip install sherpa-onnx",
])
def test_normal_commands_not_blocked(cmd):
  # 防误杀：黑名单不能宽到把日常命令全挡掉
  assert _dangerous(cmd) is False, f"正常命令被误杀: {cmd!r}"


def test_dangerous_rejects_empty():
  home = tempfile.mkdtemp()
  st = SystemTools(home)
  st.perms.save({"terminal": True, "fs": True, "web_fetch": True})
  with pytest.raises(ValueError):
    st.run_command("  ")


# ---------------------------------------------------------------- 路径越界

def _mk_evil_tree():
  base = tempfile.mkdtemp()
  home = os.path.join(base, ".omegaforge")
  os.makedirs(home)
  evil = os.path.join(base, ".omegaforge_evil")   # 同级且同前缀
  os.makedirs(evil)
  with open(os.path.join(evil, "secret.txt"), "w", encoding="utf-8") as f:
    f.write("LEAKED")
  return home


@pytest.mark.parametrize("rel", [
  "../.omegaforge_evil/secret.txt",  # 历史实现可被此绕过
  "../../etc/hostname",
  "../../etc/passwd",
])
def test_path_escape_blocked(rel):
  home = _mk_evil_tree()
  st = SystemTools(home)
  st.perms.save({"terminal": True, "fs": True, "web_fetch": True})
  with pytest.raises(ValueError) as e:
    st.fs_read(rel)
  assert "escapes workspace" in str(e.value)


def test_path_within_workspace_allowed():
  home = _mk_evil_tree()
  st = SystemTools(home)
  st.perms.save({"terminal": True, "fs": True, "web_fetch": True})
  # 门禁上线后，测试必须显式声明执行模式（默认 confirm 下写入需确认）
  st.policy.set_mode("full")
  st.fs_write("notes/a.txt", "hello")
  got = st.fs_read("notes/a.txt")
  assert got["text"] == "hello", "工作区内的正常读写不应被拦截"


# ---------------------------------------------------------------- SSRF

@pytest.mark.parametrize("url", [
  "http://127.0.0.1:8787/api/status",
  "http://localhost:8787/",
  "http://169.254.169.254/latest/meta-data/",  # 云元数据
  "http://[::1]/",
  "http://10.0.0.5/",
  "http://192.168.1.1/",
  "http://0.0.0.0/",
])
def test_internal_url_blocked(url):
  with pytest.raises(ValueError):
    _guard_public_url(url)


def test_non_http_scheme_rejected():
  """scheme 校验在 web_fetch 入口，而不是 _guard_public_url。

  这里验证过：_guard_public_url("ftp://example.com/x") 不报错是**正确的**——
  example.com 是公网地址，guard 只管内外网，scheme 由上层拦。
  """
  with pytest.raises(ValueError):
    _guard_public_url("file:///etc/passwd")
  home = tempfile.mkdtemp()
  st = SystemTools(home)
  st.perms.save({"terminal": True, "fs": True, "web_fetch": True})
  for bad in ("ftp://example.com/x", "file:///etc/passwd"):
    with pytest.raises(ValueError):
      st.web_fetch(bad)


# ---------------------------------------------------------------- 默认关闭

def test_permissions_default_all_off():
  home = tempfile.mkdtemp()
  st = SystemTools(home)
  perms = st.perms.load()
  assert all(v is False for v in perms.values()), "权限必须默认全关"
  for fn, args in ((st.run_command, ("echo hi",)),
           (st.fs_read, ("a.txt",)),
           (st.fs_list, ("." ,)),
           (st.web_fetch, ("https://example.com",))):
    with pytest.raises(PermissionError):
      fn(*args)
