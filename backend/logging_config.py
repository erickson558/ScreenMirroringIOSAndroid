from __future__ import annotations

import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler


def configure_logging(log_file: Path, level: str = "INFO", enable_console: bool = False) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if root_logger.handlers:
        root_logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=1_500_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    if enable_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
