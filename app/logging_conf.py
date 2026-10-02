"""统一日志配置。

遵循项目约定：标准 logging，格式包含时间 / 级别 / 模块 / 消息，
级别可通过环境变量 ``NTE_LOG_LEVEL`` 调整（默认 INFO）。
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 第三方库默认降噪，避免刷屏
_NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "uvicorn.access": logging.WARNING,
}


def setup_logging(level: str | None = None) -> None:
    """初始化根 logger，重复调用无副作用。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved = (level or os.getenv("NTE_LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))

    for name, lvl in _NOISY.items():
        logging.getLogger(name).setLevel(lvl)

    _CONFIGURED = True
    logging.getLogger("nte").debug("日志系统已初始化，级别=%s", resolved)


def get_logger(name: str) -> logging.Logger:
    """获取带 ``nte.`` 前缀的 logger。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"nte.{name}")
