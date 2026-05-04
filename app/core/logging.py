"""
app/core/logging.py
────────────────────
Structured logging setup for the entire application.
All modules import `logger` from here for consistent formatting.
"""

import logging
import sys
from app.core.config import get_settings

settings = get_settings()


def setup_logging() -> logging.Logger:
    """
    Configure root logger with:
    - Timestamped, leveled, named log lines
    - stdout output (suitable for Docker / cloud log collectors)
    - DEBUG level in dev, INFO in production
    """
    log_level = logging.DEBUG if settings.debug else logging.INFO

    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
    )

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Silence noisy third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return logging.getLogger("generator_platform")


logger = setup_logging()
