"""CLI surfacing of the awaiting-human attention flag (#657)."""

from __future__ import annotations

import httpx
import pytest

from awf.cli import workspace_commands
from awf.cli.common import OutputFormat


@pytest.mark.unit
def test_format_attention_line_for_flagged_payload() -> None:
    line = workspace_commands.format_attention_line(
        {
            "attention_required": True,
            "awaiting_human_reason": "blocking review requires a human",
            "awaiting_human_since": "2026-06-20T09:00:00+00:00",
        }
    )
    assert line == (
        "⚠ awaiting human: blocking review requires a human (since 2026-06-20T09:00:00+00:00)"
    )


@pytest.mark.unit
def test_format_attention_line_without_since() -> None:
    line = workspace_commands.format_attention_line(
        {"attention_required": True, "awaiting_human_reason": "merge BLOCKED"}
    )
    assert line == "⚠ awaiting human: merge BLOCKED"


@pytest.mark.unit
def test_format_attention_line_none_when_not_flagged() -> None:
    assert workspace_commands.format_attention_line({"attention_required": False}) is None
    assert workspace_commands.format_attention_line({}) is None


@pytest.mark.unit
def test_attention_marker_for_flagged_row() -> None:
    marker = workspace_commands.attention_marker(
        {
            "attention_required": True,
            "workspace_id": "ws_abc",
            "awaiting_human_reason": "needs a human",
        }
    )
    assert marker == "⚠ ws_abc awaiting human: needs a human"


@pytest.mark.unit
def test_attention_marker_empty_when_not_flagged() -> None:
    assert workspace_commands.attention_marker({"attention_required": False}) == ""
    assert workspace_commands.attention_marker({}) == ""


@pytest.mark.unit
def test_workspace_show_pretty_emits_attention_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "id": "ws_abc",
        "status": "monitoring_pr",
        "attention_required": True,
        "awaiting_human_reason": "blocking review requires a human",
        "awaiting_human_since": "2026-06-20T09:00:00+00:00",
    }

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(workspace_commands, "_call", _call)

    workspace_commands.workspace_show(
        workspace_id="ws_abc",
        api_token="token",
        base_url=None,
        fmt=OutputFormat.pretty,
    )

    out = capsys.readouterr().out
    assert "⚠ awaiting human: blocking review requires a human" in out


@pytest.mark.unit
def test_workspace_show_json_carries_fields_without_decorated_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "id": "ws_abc",
        "status": "monitoring_pr",
        "attention_required": True,
        "awaiting_human_reason": "blocking review",
        "awaiting_human_since": "2026-06-20T09:00:00+00:00",
    }

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(workspace_commands, "_call", _call)

    workspace_commands.workspace_show(
        workspace_id="ws_abc",
        api_token="token",
        base_url=None,
        fmt=OutputFormat.json,
    )

    out = capsys.readouterr().out
    # JSON output carries the raw fields; the decorated ⚠ line is pretty-only.
    assert '"attention_required": true' in out
    assert "⚠ awaiting human" not in out


@pytest.mark.unit
def test_echo_attention_line_ignores_non_json_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A pretty 200 whose body is not valid JSON must not crash the CLI and must
    # not emit an attention line — the decorated line is best-effort sugar.
    response = httpx.Response(200, content=b"<<not json>>")

    workspace_commands._echo_workspace_attention_line(response, OutputFormat.pretty)

    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_echo_attention_line_ignores_non_mapping_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Valid JSON that is not an object (e.g. a list) is skipped, not rendered.
    response = httpx.Response(200, json=["not", "a", "mapping"])

    workspace_commands._echo_workspace_attention_line(response, OutputFormat.pretty)

    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_echo_list_attention_ignores_non_json_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The per-row marker path is equally defensive against a non-JSON list body.
    response = httpx.Response(200, content=b"oops not json")

    workspace_commands._echo_workspace_list_attention(response, OutputFormat.pretty)

    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_workspace_list_pretty_emits_row_markers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "items": [
            {
                "workspace_id": "ws_flagged",
                "status": "monitoring_pr",
                "attention_required": True,
                "awaiting_human_reason": "needs a human",
            },
            {
                "workspace_id": "ws_clean",
                "status": "running",
                "attention_required": False,
            },
        ]
    }

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(workspace_commands, "_call", _call)

    workspace_commands.workspace_list(
        status=None,
        agent=None,
        repo_url=None,
        limit=50,
        api_token="token",
        base_url=None,
        fmt=OutputFormat.pretty,
    )

    out = capsys.readouterr().out
    assert "⚠ ws_flagged awaiting human: needs a human" in out
    assert "ws_clean awaiting human" not in out
