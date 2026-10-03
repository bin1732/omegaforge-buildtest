"""前端构建契约守卫。

背景——前端构建产物已纳入校验
``tsc --noEmit`` + ``vite build``。跑通过程中暴露出两类静默失配：

1. ``package-lock.json`` 与 ``package.json`` 分叉：
  ``radix-ui`` / ``lucide-react`` 在 manifest 里声明了，lock 里
  **整个条目不存在**（157 vs 243 entries）。后果是 npm 装出一份
  "看起来成功、实际缺 79 个子依赖"的 node_modules，构建必然失败，
  而失败原因会被误读成"网络问题"。

2. 端口三处一致是注释里的**口头承诺**，没有任何占位：
  server.py 的 ``serve()`` 默认端口、src-tauri 的 ``BACKEND_PORT``、
  api.ts 的 ``BASE``。任一处改了另外两处不会报错，界面会静默
  连不上后端。

这些都不在既有前端契约套件的覆盖内（那只扫源码里的路径字面量）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _read(rel: str) -> str:
  return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------
# 1. manifest 与 lockfile 不得分叉
# ---------------------------------------------------------------

def test_every_manifest_dep_present_in_lockfile():
  """package.json 里声明的每个依赖，在 lock 里必须有实体条目。

  这是本次改动验证撞到的失效：缺的不是版本号对不上，而是**整个条目缺失**，
  于是 npm 装完不报错、node_modules 里也没有该包。
  """
  if not (FRONTEND / "package.json").exists():
    pytest.skip("frontend/package.json 不存在")
  manifest = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
  lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))

  deps: dict[str, str] = {}
  deps.update(manifest.get("dependencies", {}))
  deps.update(manifest.get("devDependencies", {}))
  assert deps, "manifest 里没有任何依赖，用例无从校验"

  entries = lock.get("packages", {})
  missing = [n for n in deps if f"node_modules/{n}" not in entries]
  assert not missing, f"lock 中缺失这些依赖的条目（manifest 与 lock 分叉）: {missing}"


def test_lockfile_is_v3_and_has_resolved_urls():
  """lock 必须是 v3 且带 resolved——否则离线复现不可行。"""
  lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
  assert lock.get("lockfileVersion") == 3, "lockfileVersion 应为 3"
  entries = lock.get("packages", {})
  with_url = [k for k, v in entries.items() if v.get("resolved")]
  assert len(with_url) > 100, f"带 resolved 的条目过少（{len(with_url)}），lock 可能不完整"


# ---------------------------------------------------------------
# 2. 端口三处一致（注释里的承诺，缺少该约束时无占位）
# ---------------------------------------------------------------

def _server_default_port() -> str:
  src = _read("omegaforge/server.py")
  m = re.search(r"def serve\([^)]*port:\s*int\s*=\s*(\d+)", src)
  assert m, "server.py 中找不到 serve() 的默认端口"
  return m.group(1)


def _tauri_port() -> str:
  src = _read("src-tauri/src/main.rs")
  m = re.search(r"const\s+BACKEND_PORT:\s*u16\s*=\s*(\d+)", src)
  assert m, "main.rs 中找不到 BACKEND_PORT"
  return m.group(1)


def _api_base_port() -> str:
  src = _read("frontend/src/lib/api.ts")
  m = re.search(r"const\s+DEFAULT_BASE\s*=\s*['\"]http://127\.0\.0\.1:(\d+)['\"]", src)
  assert m, "api.ts 中找不到默认后端地址定义"
  return m.group(1)


def test_backend_port_consistent_across_three_sites():
  """server.py / main.rs / api.ts 三处端口必须一致。

  api.ts 的注释明确承诺"三处一致"，缺少该约束时没有任何占位。任一处漂移，
  界面会静默连不上后端——表现为"点了没反应"，而不是报错。
  """
  ports = {
    "server.py": _server_default_port(),
    "main.rs": _tauri_port(),
    "api.ts": _api_base_port(),
  }
  assert len(set(ports.values())) == 1, f"端口三处不一致: {ports}"


def test_api_base_is_loopback_only():
  """前端 BASE 必须指向回环地址。

  若改成 0.0.0.0 或局域网地址，等于把只监听 127.0.0.1 的后端
  暴露给同网段——与本项目的本地服务定位相悖。
  """
  src = _read("frontend/src/lib/api.ts")
  m = re.search(r"const\s+DEFAULT_BASE\s*=\s*['\"]([^'\"]+)['\"]", src)
  assert m, "api.ts 中找不到默认后端地址定义"
  assert m.group(1).startswith("http://127.0.0.1:"), f"默认地址必须绑定回环: {m.group(1)}"
  # 可覆盖不等于可以写死别的地址：文件里出现的任何地址字面量都必须是回环
  for lit in re.findall(r"['\"](https?://[^'\"]+)['\"]", src):
    assert lit.startswith("http://127.0.0.1:") or lit.startswith("https://127.0.0.1:"), \
        f"出现非回环地址字面量: {lit}"


# ---------------------------------------------------------------
# 3. 构建配置自洽
# ---------------------------------------------------------------

def test_vite_alias_points_at_src():
  """@/ 别名必须指向 src——否则全部 "@/..." 引用静默失效。"""
  src = _read("frontend/vite.config.ts")
  assert '"./src"' in src or "'./src'" in src, "vite alias 未指向 ./src"


def test_build_script_runs_typecheck():
  """build 必须先过 tsc --noEmit。

  只跑 vite build 的话，类型错误会被 esbuild 静默忽略（它不做类型检查），
  产物带着类型错误上线。
  """
  manifest = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
  build = manifest.get("scripts", {}).get("build", "")
  assert "tsc" in build, f"build 脚本未包含类型检查: {build}"
