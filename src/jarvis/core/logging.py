import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru with stderr output and rotating file log."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | {level} | {message}",
    )
    Path("logs").mkdir(exist_ok=True)
    logger.add(
        "logs/jarvis_{time:YYYY-MM-DD}.log",
        rotation="10 MB",
        retention="30 days",
        compression="zip",
        enqueue=True,
        level=level,
        encoding="utf-8",
    )