"""Structured JSON logging via structlog.

Logs are always JSON lines on stdout — even in local dev — so we never write
code that depends on human-readable format, and log parsing works the same
everywhere. The processor chain adds:

- ISO 8601 timestamp
- Log level
- Service name (from Settings)
- Source logger name
- Any kwargs passed to the log call

Call ``configure_logging()`` once at process start; subsequent calls are idempotent.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from awf.common.config import Settings

_configured = False


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging to emit JSON lines on stdout.

    Idempotent so tests and app startup can both call it safely.
    """
    global _configured
    if _configured:
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.EventRenamer("message"),
        structlog.processors.CallsiteParameterAdder(
            parameters={
                structlog.processors.CallsiteParameter.MODULE,
                structlog.processors.CallsiteParameter.FUNC_NAME,
            }
        ),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Bind service name as a permanent context var so every log carries it.
    structlog.contextvars.bind_contextvars(service=settings.service_name, env=settings.env)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger. Prefer ``get_logger(__name__)`` per module."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
