"""路径解析的唯一真源。

为什么需要这个模块
--------------------
目录位置曾经分散在多个模块里各自解析，其中一部分在实例创建时就把
路径固定下来，另一部分每次使用时才解析。

分散解析本身不致命，致命的是在实例创建期求值：

    self.home = home or os.getenv(...)          # import 时固化
    self.path = os.path.join(self.home, "kb.json")

而 `server.py` 里这些全是**模块级单例**（`KB = KnowledgeBase()` 等），
于是只要该模块被 import 过一次，此后任何「改了 OMEGAFORGE_HOME 再复用
该模块」的场景都会读错目录。

（本版定位，fork 场景）：

    RUNS.home    = /data/workspace/LATEST/.omegaforge      ✗ 仍指向旧目录
    KB.path      = .../LATEST/.omegaforge/kb.json          ✗
    TASKS.path   = .../LATEST/.omegaforge/tasks.json       ✗
    USAGE.path   = .../LATEST/.omegaforge/usage.jsonl      ✗
    CONVS.dir    = .../LATEST/.omegaforge/conversations    ✗
    PROVIDERS    = .../LATEST/.omegaforge/providers.json   ✗
    SYSTOOLS     = /data/workspace/LATEST/.omegaforge      ✗

表现是「单独跑全绿、整批跑失败」——失败与代码无关，只与执行顺序有关。
更糟的是它会**写进真实 home**：子进程以为自己在临时目录，实际写的是
仓库下的 `.omegaforge/`。

修法是让路径**惰性**：`home` 变 property，每次访问重新解析；所有派生
路径（path / dir / runs_dir）也变只读 property，从 `home` 现算。
这样环境变了就跟着变，fork 继承也不再携错误目录。

统一到本模块的第二个理由：`_home()` 分散在 5 个文件里，是将来分叉的
温床。现在只有一个实现。
"""

from __future__ import annotations

import os
import sys

_ENV = "OMEGAFORGE_HOME"
_DEFAULT_DIRNAME = ".omegaforge"


def packaged_home() -> str:
    """打包运行时的数据根目录：用户目录下的固定位置。

    打包后的后端由桌面外壳拉起，工作目录取决于外壳的启动方式——从开始
    菜单、快捷方式、命令行启动都可能不同。落到当前目录意味着数据会写进
    安装目录，而卸载会整棵删除安装目录：用户积累的知识库、待办、蒸馏产
    物随之消失，且卸载界面不会提示。故打包运行一律落到用户目录。
    """
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return os.path.join(base, "OmegaForge")
    return os.path.join(os.path.expanduser("~"), _DEFAULT_DIRNAME)


def resolve_home(explicit: str | None = None) -> str:
    """解析数据根目录：显式传入 > 环境变量 > 当前目录下的 .omegaforge。

    每次调用都重新读取环境变量——这是「惰性」的关键，不要在 import 期
    把结果固化到实例属性上。
    """
    if explicit:
        return explicit
    env = os.getenv(_ENV)
    if env:
        return env
    # 打包运行时不再退回当前目录：安装目录会被卸载整棵删除。
    if getattr(sys, "frozen", False):
        return packaged_home()
    return os.path.join(os.getcwd(), _DEFAULT_DIRNAME)


def join_home(explicit: str | None, *parts: str) -> str:
    """在解析出的根目录下拼接子路径。"""
    return os.path.join(resolve_home(explicit), *parts)


class LazyHome:
    """惰性 home 的共享实现，供所有模块级单例继承。

    只把 `home` 改成 property 会引入一个**新的**不一致：那些在 `__init__`
    里把磁盘内容读进内存的单例（KB / Tasks / Usage / Providers），路径会
    跟着环境变量变，内存里的数据却还是旧目录的——于是"往新 home 写"时
    写的是旧 home 读来的内容。

    固化时期二者始终指向同一个目录，反而不会出现这种错配。所以惰性化
    必须连"数据跟随"一起做：记住加载时的 home，访问时发现变了就重加载。

    子类需要：
      - 在 `__init__` 里先 `super().__init__(home)`
      - 实现 `_reload()`：按当前 `self.home` 重新读盘
      - 在**每次**读 `_docs` 这类缓存前调 `ensure_loaded()`
    """

    def __init__(self, home: str | None = None) -> None:
        self._explicit = home or None
        self._loaded_home: str | None = None

    @property
    def home(self) -> str:
        return resolve_home(self._explicit)

    @home.setter
    def home(self, value: str | None) -> None:
        # 兼容既有测试的 `srv.RUNS.home = tmp` 写法；赋值即视为显式指定。
        self._explicit = value or None
        self._loaded_home = None

    def ensure_loaded(self) -> None:
        """home 变了或从未加载过就重读一次。"""
        h = self.home
        if self._loaded_home != h:
            self._reload()
            self._loaded_home = h

    def invalidate(self) -> None:
        """丢弃内存缓存，下次访问时重新读盘。

        为什么需要这个方法：
        `ensure_loaded` 只在 **home 变化**时重读。而测试里有一类场景是
        **同一个 home 下的文件内容被外部改写**（手工写坏 kb.json 以模拟
        历史脏数据）。若这类用例靠 `del sys.modules["omegaforge*"]`
        强制重建单例——但那会造成**类身份分裂**：

            test_mcp_limits.py 在模块级 `from ... import UserError` 绑定了
            旧类对象；运行时重新导入后抛出的是新类对象，于是
            `assertRaises(UserError)` 永远捕获不到，表现为「单独跑全绿、
            整批跑失败」。

        有了本方法，测试应调 `KB.invalidate()` 而不是删 sys.modules——
        语义更准，且不产生两份类对象。
        """
        self._loaded_home = None

    def _reload(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError
