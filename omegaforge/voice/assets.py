"""语音模型根目录解析：内置优先，数据目录兜底。

## 为什么单独成模块

asr 与 tts 各自写过一份 `_models_root()`，都用

    Path(__file__).resolve().parent.parent.parent / "models"

推导"随包内置的模型目录"。这个推导在源码态下指向仓库根/models，是对的；
但 PyInstaller onedir 打包后，模块文件位于

    <安装目录>/_internal/omegaforge/voice/asr.py

于是 parent.parent.parent 落在 `_internal`，推导结果变成 `_internal/models`。
而 Tauri resources 把资源放在**安装目录根**，即 `<安装目录>/models`。

两个位置不一致，而失效症状与"压根没内置"完全一样：status 报
files_missing、界面显示"未安装"、用户点一键安装去下载本来就在磁盘上的东西。
内置了模型却找不到，等于白内置，且从任何一层验收上都看不出来——
接口全绿、status 正确、下载器也能跑通，只有用户启不动。

因此解析逻辑只在这里定义一次，两侧共用，并且**显式区分打包态**，
不再靠 `__file__` 的相对层级推导。

## 查找顺序（先内置、后兜底）

1. 打包态：`sys.executable` 同级 / `_internal/models`
2. 打包态：`sys.executable` 同级 / `models`
3. 源码态：仓库根 / `models`（`omegaforge/` 的上一级）
4. 兜底：数据目录 / `models`（用户自行下载安装的）

命中即返回，并同时返回来源标记——内置与用户装的两份同时存在时，
没有来源标记就无法判断用户实际在用哪一份。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import spec

#: 模型目录名。asr 与 tts 的各自子目录都挂在它下面。
MODELS_DIRNAME = "models"


def repo_root() -> Path:
    """源码态：omegaforge/ 的上一级。"""
    return Path(__file__).resolve().parents[2] / ".."


def candidates() -> list[tuple[str, Path]]:
    """按优先级返回 (来源标记, 候选目录)。

    来源标记会出现在 status 里，用于诊断"用户实际在用哪一份"。
    """
    out: list[tuple[str, Path]] = []

    # 打包态：exe 位于安装目录根，resources 亦在安装目录根。
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        out.append(("bundled-internal", exe.parent / "_internal" / MODELS_DIRNAME))
        out.append(("bundled", exe.parent / MODELS_DIRNAME))

    # 源码态：仓库根/models
    try:
        src = Path(__file__).resolve().parents[3] / MODELS_DIRNAME
    except IndexError:
        src = None
    if src is not None:
        out.append(("source", src))

    # 兜底：数据目录/models
    from omegaforge.core.paths import resolve_home
    out.append(("home", Path(resolve_home()) / MODELS_DIRNAME))
    return out


def has_model(root: Path, model_name: str) -> bool:
    """该目录下是否真的有这个模型（必需条目齐备）。

    只判"目录里有任意文件"是不够的：目录里只有另一版本的主文件、却缺必需条目时，
    宽松判定会让"有这个模型"成立，随包内置的可用那份随即被跳过，界面
    显示"未安装"——用户装过却要重下一次。因此按必需条目齐备判定，
    判据见 voice/spec.dir_has_kind。
    """
    kind = spec.kind_of_model(model_name)
    if kind:
        return spec.dir_has_kind(root, kind)
    d = root / model_name
    if not d.is_dir():
        return False
    try:
        return any(p.is_file() for p in d.rglob("*"))
    except OSError:
        return False


def resolve(model_name: Optional[str] = None) -> tuple[Path, str]:
    """返回 (模型根目录, 来源标记)。

    model_name 给定时会跳过不含该模型的候选——否则命中一个空的内置目录
    后就不会再往下找数据目录里用户真正装好的那份。
    """
    for tag, root in candidates():
        if model_name is None or has_model(root, model_name):
            return root, tag
    # 全部不含该模型时，仍返回兜底目录，让 status 能报出"缺什么"。
    from omegaforge.core.paths import resolve_home
    return Path(resolve_home()) / MODELS_DIRNAME, "home"
