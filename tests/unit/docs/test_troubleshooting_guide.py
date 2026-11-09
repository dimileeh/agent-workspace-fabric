from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GETTING_STARTED_PATH = REPO_ROOT / "docs" / "GETTING_STARTED.md"
TROUBLESHOOTING_PATH = REPO_ROOT / "docs" / "TROUBLESHOOTING.md"


@pytest.mark.unit
def test_getting_started_links_to_first_run_troubleshooting() -> None:
    """Assert getting-started points readers at the first-run troubleshooting section."""
    content = GETTING_STARTED_PATH.read_text(encoding="utf-8")
    assert any(
        link in content
        for link in (
            "docs/TROUBLESHOOTING.md#first-run-troubleshooting",
            "TROUBLESHOOTING.md#first-run-troubleshooting",
        )
    )


@pytest.mark.unit
def test_getting_started_first_run_troubleshooting_anchor_exists() -> None:
    """Assert the first-run troubleshooting link points to an existing heading."""
    troubleshooting_content = TROUBLESHOOTING_PATH.read_text(encoding="utf-8")
    assert "## First-run troubleshooting" in troubleshooting_content


@pytest.mark.unit
def test_troubleshooting_sections_cover_required_symptoms() -> None:
    """Assert the troubleshooting document includes all required symptom sections."""
    content = TROUBLESHOOTING_PATH.read_text(encoding="utf-8")

    required_sections = (
        "## Symptom: service bootstrap command fails",
        "## Symptom: Postgres is unavailable, is recovering, or disk is full",
        "## Symptom: Docker daemon/socket/Compose unavailable",
        "## Symptom: Agent runtime image is missing",
        "## Symptom: Provider auth/preflight failure",
        "## Symptom: GitHub auth failure",
        "## Symptom: `/readyz` reports warnings or 503",
        "## Symptom: Workspace is stuck or repeatedly fails",
        "## Where to find logs and evidence",
    )

    for section in required_sections:
        assert section in content, f"Missing troubleshooting section heading: {section}"


@pytest.mark.unit
def test_troubleshooting_guide_has_actionable_commands() -> None:
    """Assert the troubleshooting guide documents required operational commands."""
    content = TROUBLESHOOTING_PATH.read_text(encoding="utf-8")

    required_commands = (
        "awf init <path>",
        "awf service bootstrap",
        "awf service status",
        "awf service doctor",
        "awf service logs",
        "curl http://localhost:8000/readyz",
        "awf service status --provider codex",
        "gh auth status",
        "awf workspace show",
        "awf workspace events",
        "awf workspace operations",
        "awf workspace logs",
        "awf workspace runtime",
        "awf workspace log <workspace_id> <stream_id>",
        "- `GET /v1/workspaces/{workspace_id}/logs`",
        "- `GET /v1/workspaces/{workspace_id}/logs/{stream_id}`",
        "GET /v1/workspaces/{workspace_id}/events",
        "GET /v1/workspaces/{workspace_id}/operations",
        "GET /v1/workspaces/{workspace_id}/runtime",
    )

    for required_command in required_commands:
        assert required_command in content, (
            f"Troubleshooting guide missing actionable command: {required_command}"
        )


def _agent_runtime_download_section(content: str) -> str:
    """Return the normalized text of the agent-runtime image troubleshooting section."""
    start = content.index("## Symptom: Agent runtime image is missing")
    end = content.index("## Symptom:", start + 1)
    return " ".join(content[start:end].split())


@pytest.mark.unit
def test_agent_runtime_download_troubleshooting_reads_curl_errors_first() -> None:
    """Assert the download-failure advice branches on the preceding curl errors."""
    section = _agent_runtime_download_section(TROUBLESHOOTING_PATH.read_text(encoding="utf-8"))

    assert "Read the `curl:` lines" in section
    assert "This is a GitHub-side outage, not a bad pin" not in section
    assert "curl: (22)" in section and "504" in section
    assert "temporary outage" in section
    assert "404" in section and "pinned version" in section and "asset URL" in section
    assert "certificate" in section and "proxy" in section
    assert "checksum mismatch" in section and "Never bypass" in section
    assert "GitHub CLI <asset> could not be downloaded. Sources tried:" in section
    assert "Antigravity CLI <asset> could not be downloaded. Sources tried:" in section
