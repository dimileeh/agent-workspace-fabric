"""Focused coverage for MCP setup validation and instruction rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.host_setup.clients import ClientConfigPlan
from awf.host_setup.rendering import FirstRunIssue, FirstRunPayload, FirstRunRemediation
from awf.mcp import setup_tools


def _safe_result(
    payload: dict[str, Any],
    *,
    is_error: bool = False,
    extra_secrets: object = (),
) -> Any:
    return {"payload": payload, "is_error": is_error, "extra_secrets": tuple(extra_secrets)}


def _remediation(*, command: str | None) -> FirstRunRemediation:
    return FirstRunRemediation(
        problem="problem",
        cause="cause",
        fix="fix",
        docs_link="https://example.test/docs",
        related_command=command,
    )


def _issue(*, reason_code: str, command: str | None) -> FirstRunIssue:
    return FirstRunIssue(
        reason_code=reason_code,
        severity="failed",
        remediation=_remediation(command=command),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_local_service_rejects_conflicting_build_options() -> None:
    """MCP start cannot request a full rebuild while skipping the runtime build."""

    result = await setup_tools._start_local_service_result(
        safe_result=_safe_result,
        rebuild=True,
        skip_agent_runtime_build=True,
        timeout_seconds=30,
        source_checkout=None,
    )

    assert result["is_error"] is True
    assert result["payload"]["error_code"] == setup_tools.START_OPTIONS_INVALID


@pytest.mark.unit
def test_settings_secret_field_names_handles_objects_without_dicts() -> None:
    """Slot-only settings-like objects expose no dynamic secret fields."""

    class _SlotOnly:
        __slots__ = ()

    assert setup_tools._settings_secret_field_names(_SlotOnly()) == ()


@pytest.mark.unit
def test_project_profile_init_rejects_file_path(tmp_path: Path) -> None:
    """MCP project initialization reports files as invalid repository paths."""

    project_file = tmp_path / "project.txt"
    project_file.write_text("not a directory\n", encoding="utf-8")

    result = setup_tools._initialize_project_profile_result(
        safe_result=_safe_result,
        project_path=str(project_file),
        include_smoke_request=False,
        write_profile=False,
        template="generic",
        force=False,
    )

    assert result["is_error"] is True
    assert result["payload"]["error_code"] == setup_tools.PROJECT_INIT_INVALID_PATH
    assert "not a directory" in result["payload"]["message"]


@pytest.mark.unit
def test_client_instruction_payload_includes_cli_and_conflict_details(tmp_path: Path) -> None:
    """Instruction payloads preserve optional official-CLI and conflict evidence."""

    plan = ClientConfigPlan(
        client="claude",
        method="official_cli",
        config_path=tmp_path / "claude.json",
        action="conflict",
        conflict_detail="existing AWF entry differs",
        desired_entry={"command": "awf", "args": ["mcp", "serve"]},
        cli_command=("claude", "mcp", "add", "awf"),
    )

    payload = setup_tools._client_instruction_payload(plan, source_checkout=None)

    assert payload["client_cli_command"] == ["claude", "mcp", "add", "awf"]
    assert payload["conflict_detail"] == "existing AWF entry differs"


@pytest.mark.unit
def test_client_instruction_blocked_summary_and_next_steps(tmp_path: Path) -> None:
    """Conflict plans render an explicit blocked summary and remediation step."""

    plan = ClientConfigPlan(
        client="claude",
        method="file",
        config_path=tmp_path / "claude.json",
        action="conflict",
    )

    assert "conflicts" in setup_tools._client_instructions_summary([plan], blocked=True)
    assert setup_tools._client_instruction_next_steps(
        [plan],
        blocked=True,
        source_checkout=None,
    ) == ["Resolve the conflicting client config entries, then re-run this MCP instruction tool."]


@pytest.mark.unit
def test_client_source_checkout_issue_preserves_unrelated_remediations() -> None:
    """Only source-checkout issues with source-checkout commands are rewritten."""

    unrelated_reason = _issue(reason_code="OTHER_REASON", command="awf setup --source-checkout /x")
    unrelated_command = _issue(
        reason_code=setup_tools.SOURCE_CHECKOUT_INVALID,
        command="awf setup --dry-run",
    )

    assert (
        setup_tools._client_source_checkout_issue_with_command(
            unrelated_reason,
            command="awf setup --client claude",
        )
        is unrelated_reason
    )
    assert (
        setup_tools._client_source_checkout_issue_with_command(
            unrelated_command,
            command="awf setup --client claude",
        )
        is unrelated_command
    )


@pytest.mark.unit
def test_client_source_checkout_payload_without_source_or_issues_stays_minimal() -> None:
    """Blocked payloads without issue entries do not synthesize issue rewrites."""

    payload = FirstRunPayload(
        status="blocked",
        command="awf setup",
        summary="source checkout invalid",
    )

    updated = setup_tools._client_source_checkout_blocked_payload_with_explicit_command(
        payload,
        selected_clients=["claude"],
        source_checkout=None,
    )

    assert updated.issues == ()
    assert updated.command == "awf setup --client claude"


@pytest.mark.unit
def test_client_env_file_missing_prefers_explicit_source_checkout(tmp_path: Path) -> None:
    """An explicit source checkout wins without loading persisted host setup config."""

    source_checkout = tmp_path / "checkout"
    assert (
        setup_tools._client_env_file_missing_source_checkout(
            source_checkout,
            tmp_path / ".env",
        )
        == source_checkout
    )


@pytest.mark.unit
def test_client_reason_coded_issue_preserves_non_setup_command() -> None:
    """Remediations unrelated to setup/start are not rewritten to the MCP command."""

    issue = _issue(reason_code="OTHER_REASON", command="git status")

    assert (
        setup_tools._client_reason_coded_issue_with_command(
            issue,
            command="awf setup --client claude",
        )
        is issue
    )
