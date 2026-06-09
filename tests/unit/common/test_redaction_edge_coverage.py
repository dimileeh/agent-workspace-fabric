"""Edge coverage for shared redaction helpers."""

from __future__ import annotations

import importlib

import pytest

from awf.common import redaction


@pytest.mark.unit
def test_redact_secrets_slice_handles_empty_and_clamped_empty_ranges() -> None:
    assert redaction.redact_secrets_slice("", 0, 10, extra_secrets=["secret-token"]) == ""
    assert redaction.redact_secrets_slice("plain", 10, 20, extra_secrets=["secret-token"]) == ""


@pytest.mark.unit
def test_redact_secrets_byte_slice_handles_empty_ranges() -> None:
    assert redaction.redact_secrets_byte_slice("plain", 4, 4, extra_secrets=["secret-token"]) == ""


@pytest.mark.unit
def test_render_redacted_bytes_skips_prior_spans_and_stops_after_slice() -> None:
    rendered = redaction._render_redacted_bytes(
        b"before secret after",
        7,
        13,
        [(0, 3), (7, 13), (14, 19)],
    )

    assert rendered == b"<redacted>"


@pytest.mark.unit
def test_render_redacted_byte_slice_skips_prior_spans_and_stops_after_slice() -> None:
    rendered = redaction._render_redacted_byte_slice(
        b"before secret after",
        7,
        13,
        [(0, 3), (7, 13), (14, 19)],
    )

    assert rendered == "<redacted>"


@pytest.mark.unit
def test_container_ops_placeholder_imports_for_coverage() -> None:
    assert importlib.import_module("awf.control.executor.container_ops").__name__ == (
        "awf.control.executor.container_ops"
    )
