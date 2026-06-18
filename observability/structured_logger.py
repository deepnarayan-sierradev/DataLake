"""
Structured logger for the Enterprise Data Lake platform.

Security guarantees:
  - All string values in log events are passed through _scrub_sensitive_processor
    before emission, providing a last-resort defence against credential leakage.
  - Colour codes and rich-console keys are stripped to ensure clean JSON output
    in CloudWatch Logs Insights.
  - Logging is JSON by default in all environments; human-readable console mode
    is available for local development via render_as_json=False.

Usage:
    from observability.structured_logger import configure_platform_logging, get_platform_logger

    # Call once at application startup:
    configure_platform_logging()

    # In each module:
    _logger = get_platform_logger(__name__)
    _logger.info("extraction_started", run_id=run_id, source_id=source_id, entity_id=entity_id)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict

from contracts.observability_contract import scrub_sensitive_values

# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------


def _scrub_value(value: Any) -> Any:
    """
    Recursively scrub sensitive patterns from any value in a log event.

    Handles nested dicts, lists, and tuples so that structured objects
    passed as log kwargs are fully sanitised before emission (OWASP A09).
    """
    if isinstance(value, str):
        return scrub_sensitive_values(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(v) for v in value)
    return value


def _scrub_sensitive_processor(
    logger: logging.Logger,
    method: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Scrub sensitive patterns from all values in the event dict, recursively.

    This processor is the last-resort safety net. Callers should use
    scrub_sensitive_values() before passing exception messages to the logger,
    but this processor catches anything that slips through, including nested
    dicts or lists that contain sensitive strings.
    """
    for key, value in list(event_dict.items()):
        event_dict[key] = _scrub_value(value)
    return event_dict


def _drop_internal_structlog_keys(
    logger: logging.Logger,
    method: str,
    event_dict: EventDict,
) -> EventDict:
    """Remove structlog-internal keys that must not appear in emitted events."""
    event_dict.pop("_record", None)
    event_dict.pop("_from_structlog", None)
    return event_dict


# ---------------------------------------------------------------------------
# Public configuration API
# ---------------------------------------------------------------------------


def configure_platform_logging(
    log_level: str = "INFO",
    render_as_json: bool = True,
) -> None:
    """
    Configure structlog for the platform with security-safe processors.

    Call this exactly once at application startup before any logging occurs.
    Calling it multiple times is safe but redundant after the first call.

    Args:
        log_level: Root log level. "INFO" for production; "DEBUG" for local dev.
        render_as_json: True (default) emits compact JSON suitable for CloudWatch.
                        False emits human-readable console output for local development.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _scrub_sensitive_processor,
        _drop_internal_structlog_keys,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if render_as_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Remove only StreamHandlers already writing to stdout.  Preserving all
    # other handlers (e.g. FileHandler, SysLogHandler) avoids silently
    # discarding in-progress work configured by third-party libraries (F-19).
    for existing in list(root_logger.handlers):
        is_stdout_stream = (
            isinstance(existing, logging.StreamHandler)
            and getattr(existing, "stream", None) is sys.stdout
        )
        if is_stdout_stream:
            root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))


def get_platform_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a named bound logger for the given component.

    Args:
        name: Typically __name__ of the calling module.

    Returns:
        A structlog BoundLogger bound to the given name.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
