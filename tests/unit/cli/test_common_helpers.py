"""Focused coverage for shared CLI helper branches."""

from __future__ import annotations

import httpx
import pytest
import typer

from awf.cli import common as cli_common


@pytest.mark.unit
def test_request_context_handles_response_without_request() -> None:
    assert cli_common._request_context(httpx.Response(200)) == (None, None)


@pytest.mark.unit
def test_profile_summary_helpers_cover_empty_runtime_and_scalar_edges() -> None:
    assert cli_common._profile_runtime_summary({}) == ""  # noqa: SLF001
    assert cli_common._has_positive_coverage_target(object())  # noqa: SLF001
    assert cli_common._format_coverage_target("99.5") == "99.5"  # noqa: SLF001


@pytest.mark.unit
def test_emit_profile_preview_pretty_covers_nested_summaries(capsys) -> None:
    payload = {
        "profile": {
            "name": "detected",
            "source": "template",
            "confidence": "high",
            "runtime": {"environment": {"PYTHONUNBUFFERED": "1"}},
            "services": [{"name": "postgres"}, "redis"],
            "phases": {
                "setup": [{"command": "uv sync"}, "not-a-map"],
                "validate": ["pytest -q"],
            },
            "validation": {"coverage": {"target": 0.99}},
        },
        "network_posture": {"status": "open", "reason": "bootstrap"},
        "lint_findings": [
            {"severity": "warn", "message": "first"},
            "ignored",
            {"message": "second"},
            {"severity": "info", "message": "third"},
        ],
        "reason": "ready",
    }

    cli_common._emit_profile_preview_pretty(payload)

    output = capsys.readouterr().out
    assert "Runtime: environment=1 value(s)" in output
    assert "Services: postgres, redis" in output
    assert "Setup: uv sync" in output
    assert "Coverage target: 99.0%" in output
    assert "Network posture: open (bootstrap)" in output
    assert "Profile lint: 4 finding(s)" in output
    assert "Reason: ready" in output


@pytest.mark.unit
def test_emit_profile_preview_pretty_covers_clean_and_string_variants(capsys) -> None:
    cli_common._emit_profile_preview_pretty(
        {
            "profile": {
                "name": "plain",
                "runtime": {"image": "python:3.12"},
                "services": [],
                "phases": {},
                "validation": {"coverage": {"minimum_percent": 0}},
            },
            "network_posture": "restricted",
            "lint_findings": [],
        }
    )

    output = capsys.readouterr().out
    assert "Runtime: image=python:3.12" in output
    assert "Services: none declared" in output
    assert "Validation: none declared" in output
    assert "Network posture: restricted" in output
    assert "Profile lint: clean" in output


@pytest.mark.unit
def test_emit_smoke_pretty_covers_links_phases_and_next_actions(capsys) -> None:
    cli_common._emit_smoke_pretty(
        {
            "status": "warn",
            "mode": "mocked",
            "project": "demo",
            "console_links": {"ui": "http://localhost:3000", "api_docs": "http://api/docs"},
            "phases": [
                {
                    "status": "fail",
                    "name": "validate",
                    "message": "missing pytest",
                    "reason_code": "TOOL_MISSING",
                    "action": "install pytest",
                },
                "ignored",
            ],
            "next_actions": ["No action required.", "awf init ."],
        }
    )

    output = capsys.readouterr().out
    assert "Console: http://localhost:3000" in output
    assert "API docs: http://api/docs" in output
    assert "[fail] validate: missing pytest" in output
    assert "reason: TOOL_MISSING" in output
    assert "action: install pytest" in output
    assert "awf init ." in output


@pytest.mark.unit
def test_emit_helpers_cover_scalar_and_mapping_edges(capsys) -> None:
    cli_common._emit(
        {"outer": {"inner": 1}, "items": [{"name": "first"}]}, cli_common.OutputFormat.pretty
    )
    assert (
        cli_common._profile_runtime_summary({"runtime": {"nested": {"a": 1}}})
        == "nested=1 value(s)"
    )
    assert (
        cli_common._profile_runtime_summary({"runtime": {"items": ["a", "b"]}}) == "items=2 item(s)"
    )
    assert cli_common._profile_runtime_summary({"runtime": {"empty": []}}) == "default"
    assert cli_common._profile_coverage_target({"target": ""}) is None
    assert cli_common._format_coverage_target(0.75, fractional=True) == "75.0%"

    output = capsys.readouterr().out
    assert "outer.inner: 1" in output
    assert "items[0].name: first" in output


@pytest.mark.unit
def test_parse_json_option_rejects_invalid_and_non_object(capsys) -> None:
    with pytest.raises(typer.Exit) as invalid:
        cli_common._parse_json_option("--metadata", "{")
    with pytest.raises(typer.Exit) as non_object:
        cli_common._parse_json_option("--metadata", "[]")

    assert invalid.value.exit_code == 2
    assert non_object.value.exit_code == 2
    assert "must be valid JSON" in capsys.readouterr().err


@pytest.mark.unit
def test_handle_response_covers_empty_pretty_items_and_scalar_emit(capsys) -> None:
    cli_common._handle_response(httpx.Response(204), cli_common.OutputFormat.pretty)
    cli_common._handle_response(
        httpx.Response(200, json={"items": [{"id": "one"}]}),
        cli_common.OutputFormat.pretty,
        pretty_items=True,
    )
    cli_common._emit("plain", cli_common.OutputFormat.pretty)

    output = capsys.readouterr().out
    assert "--- #1 ---" in output
    assert "id: one" in output
    assert "plain" in output


@pytest.mark.unit
def test_warn_on_overlay_unmount_failure_branches(capsys) -> None:
    # Non-dict and non-list payloads are tolerated without output.
    cli_common.warn_on_overlay_unmount_failure("not a dict")
    cli_common.warn_on_overlay_unmount_failure({"delete_errors": "not a list"})
    # Delete errors without an overlay-unmount reason code stay quiet.
    cli_common.warn_on_overlay_unmount_failure(
        {"delete_errors": [{"reason_code": "PATH_DELETE_FAILED"}, "junk"]}
    )
    assert capsys.readouterr().err == ""

    # A matching reason code prints the actionable hint to stderr.
    cli_common.warn_on_overlay_unmount_failure(
        {"delete_errors": [{"reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"}]}
    )
    err = capsys.readouterr().err
    assert "could not be unmounted" in err
    assert "CAP_SYS_ADMIN" in err
