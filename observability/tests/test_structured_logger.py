"""
Tests for the structured logger — security scrubbing in the log pipeline.
"""

from __future__ import annotations

import logging

from observability.structured_logger import (
    configure_platform_logging,
    get_platform_logger,
)


class TestConfigurePlatformLogging:
    def test_configures_without_error(self) -> None:
        configure_platform_logging(log_level="INFO", render_as_json=True)

    def test_configures_console_mode_without_error(self) -> None:
        configure_platform_logging(log_level="DEBUG", render_as_json=False)

    def test_sets_root_log_level_info(self) -> None:
        configure_platform_logging(log_level="INFO")
        assert logging.getLogger().level == logging.INFO

    def test_sets_root_log_level_debug(self) -> None:
        configure_platform_logging(log_level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_log_level_falls_back_to_info(self) -> None:
        # getattr with default INFO means unknown levels fall back
        configure_platform_logging(log_level="INVALID_LEVEL")
        assert logging.getLogger().level == logging.INFO


class TestGetPlatformLogger:
    def setup_method(self) -> None:
        configure_platform_logging(log_level="DEBUG", render_as_json=False)

    def test_returns_bound_logger(self) -> None:
        logger = get_platform_logger("test_module")
        assert logger is not None

    def test_logger_accepts_keyword_context(self) -> None:
        logger = get_platform_logger("test_module")
        # Should not raise — keyword context is standard structlog pattern
        bound = logger.bind(run_id="run-001", source_id="salesforce")
        assert bound is not None

    def test_different_names_return_different_loggers(self) -> None:
        logger_a = get_platform_logger("module_a")
        logger_b = get_platform_logger("module_b")
        # They should be distinct bound instances
        assert logger_a is not logger_b


class TestRootHandlerManagement:
    """configure_platform_logging must not duplicate stdout StreamHandlers."""

    def test_calling_twice_does_not_duplicate_stdout_handler(self) -> None:
        import sys
        configure_platform_logging(log_level="INFO", render_as_json=True)
        configure_platform_logging(log_level="INFO", render_as_json=True)
        root = logging.getLogger()
        stdout_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout
        ]
        # Should be at most 1 stdout StreamHandler after idempotent reconfiguration
        assert len(stdout_handlers) <= 1
