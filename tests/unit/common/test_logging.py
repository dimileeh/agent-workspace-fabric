"""Tests for awf.common.logging.configure_logging."""

from __future__ import annotations

import logging
from importlib import reload

import pytest

import awf.common.logging as awf_logging
from awf.common.config import Settings


@pytest.fixture(autouse=True)
def _reset_logging_module() -> None:
    """Reload the module between tests so the ``_configured`` module flag resets."""
    reload(awf_logging)


@pytest.mark.unit
def test_configure_logging_adds_stdout_handler() -> None:
    settings = Settings(
        _env_file=None,
        AWF_LOG_LEVEL="INFO",
        AWF_SERVICE_NAME="awf-test",
    )

    awf_logging.configure_logging(settings)

    root = logging.getLogger()
    assert len(root.handlers) >= 1
    assert root.level == logging.INFO


@pytest.mark.unit
def test_configure_logging_is_idempotent() -> None:
    settings = Settings(_env_file=None)
    awf_logging.configure_logging(settings)
    handler_count = len(logging.getLogger().handlers)

    awf_logging.configure_logging(settings)
    # A second call must NOT add another handler.
    assert len(logging.getLogger().handlers) == handler_count


@pytest.mark.unit
def test_get_logger_returns_bound_logger() -> None:
    settings = Settings(_env_file=None)
    awf_logging.configure_logging(settings)
    logger = awf_logging.get_logger("awf.test")
    # BoundLogger has .info / .bind — minimal structural check.
    assert hasattr(logger, "info")
    assert hasattr(logger, "bind")
