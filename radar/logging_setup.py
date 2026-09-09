"""日志配置：控制台 + 按天滚动文件。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT_LOGGER_NAME = "radar"


def setup_logging(log_dir: Path, level: str = "INFO", name: str = ROOT_LOGGER_NAME) -> logging.Logger:
    # Windows GBK 控制台下中文/特殊字符可能触发 UnicodeEncodeError，放宽为替换
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 非常规 stdout
        pass

    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in logger.handlers[:]:  # 先 close 再移除，避免文件句柄占用（Windows 下阻断滚动）
        logger.removeHandler(h)
        h.close()
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = TimedRotatingFileHandler(
        log_dir / "radar.log", when="midnight", interval=1, backupCount=14, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if not logging.getLogger().handlers:
        logging.getLogger().addHandler(sh)
    return logger


def get_logger(name: str = ROOT_LOGGER_NAME) -> logging.Logger:
    """返回 radar 日志树下的 logger；未初始化时退化为 root，保证不丢日志。"""
    logger = logging.getLogger(name)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if not root.handlers and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return logger
