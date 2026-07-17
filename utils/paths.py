"""统一的运行时目录解析。

所有运行时生成内容（数据库、缓存、日志、模型、运行态文件等）的根目录都
通过本模块解析，避免在代码各处散落硬编码的 ``"data"`` 路径。

根目录由 ``config/config.yaml`` 的 ``data_dir`` 决定（默认 ``"data"``）。
日志目录取与 ``data_dir`` 同级的 ``logs``：
  - ``data_dir = runtime/data`` → 日志目录 ``runtime/logs``
  - ``data_dir = data``         → 日志目录 ``logs``
"""
from __future__ import annotations

from pathlib import Path

import yaml

_CONFIG_PATH = Path("config/config.yaml")
_DEFAULT_DATA_DIR = "data"


def get_data_dir() -> str:
    """返回配置中的数据目录（config.yaml 的 data_dir，默认 "data"）。"""
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return str(cfg.get("data_dir") or _DEFAULT_DATA_DIR)
    except Exception:
        pass
    return _DEFAULT_DATA_DIR


def data_path(*parts) -> Path:
    """返回 data_dir 下的路径，例如 data_path("cache") -> <data_dir>/cache。"""
    return Path(get_data_dir()).joinpath(*parts)


def get_logs_dir() -> str:
    """返回日志目录（与 data_dir 同级的 logs）。"""
    parent = Path(get_data_dir()).parent
    if str(parent) in ("", "."):
        return "logs"
    return str(parent / "logs")
