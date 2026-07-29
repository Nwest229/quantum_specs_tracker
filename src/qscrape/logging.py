"""Structured logging configuration using structlog.

Configured once, at CLI startup (see ``cli.main``). Internal diagnostics --
per-source warnings, validation errors, adapter skip notices -- go through
these structured loggers. The CLI's human-readable run summary (the final
"wrote N records to ..." style report) stays a plain ``print()`` to stdout so
existing CLI UX is unchanged.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .settings import Settings


def configure_logging(settings: Settings) -> None:
    """Configure stdlib logging + structlog to emit structured logs.

    Emits JSON when ``settings.log_json`` is true, otherwise a
    human-friendly console renderer. Level is taken from ``settings.log_level``.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if settings.log_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.log_level.upper())


def get_logger(*args: Any, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger, optionally pre-bound with context values."""
    return structlog.get_logger(*args, **initial_values)  # type: ignore[no-any-return]
