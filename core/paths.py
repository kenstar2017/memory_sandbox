"""路径解析：开发态 vs PyInstaller 打包态。"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根（配置、内置文件）。"""
    if is_frozen():
        # PyInstaller onedir/onefile
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_support_dir() -> Path:
    """可写数据目录（长时记忆等）。"""
    if is_frozen() or sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "MemorySandbox"
    else:
        base = resource_root() / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_config_path() -> Path:
    bundled = resource_root() / "config.yaml"
    # 用户可覆盖：Application Support 下的 config.yaml
    user_cfg = app_support_dir() / "config.yaml"
    if user_cfg.is_file():
        return user_cfg
    return bundled


def default_persist_dir() -> Path:
    path = app_support_dir() / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path
