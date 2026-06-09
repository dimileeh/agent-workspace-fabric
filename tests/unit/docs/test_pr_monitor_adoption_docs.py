from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

REPO_ROOT = Path(__file__).resolve().parents[3]
ADOPTION_DOC_PATHS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/PR_MONITOR_ADOPTION.md",
    "docs/REST_API_REFERENCE.md",
    "docs/CLI_REFERENCE.md",
    "docs/MCP_REFERENCE.md",
    "docs/MCP_CLIENT_PARITY.md",
    "docs/CLIENT_SURFACES.md",
)


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"Expected documentation file missing: {relative_path}"
    return path.read_text(encoding="utf-8")


def _adoption_docs() -> str:
    return "\n\n".join(_read(path) for path in ADOPTION_DOC_PATHS)


def _real_rest_routes() -> set[str]:
    from awf.api.app import create_app

    app = create_app(use_lifespan=False)
    routes: set[str] = set()
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in route.methods:
                routes.add(f"{method} {route.path}")
    return routes


def _real_cli_commands(app: typer.Typer, prefix: str = "awf") -> set[str]:
    commands: set[str] = set()
    if app.registered_commands:
        for command in app.registered_commands:
            if command.name:
                commands.add(f"{prefix} {command.name}".strip())
    if app.registered_groups:
        for group in app.registered_groups:
            if group.name and group.typer_instance:
                commands |= _real_cli_commands(group.typer_instance, f"{prefix} {group.name}")
    return commands


async def _real_mcp_tools() -> set[str]:
    from awf.common.config import Settings
    from awf.mcp.server import build_mcp_server

    mcp = build_mcp_server(service=MagicMock(), settings=Settings(_env_file=None))
    return {tool.name for tool in await mcp.list_tools()}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adoption_docs_publish_real_rest_cli_and_mcp_names() -> None:
    from awf.cli.main import app as cli_app

    docs = _adoption_docs()

    assert "POST /v1/workspaces/adopt-pr" in _real_rest_routes()
    assert "awf workspace adopt-pr" in _real_cli_commands(cli_app)
    assert "awf_adopt_pull_request_monitor" in await _real_mcp_tools()

    for required in (
        "POST /v1/workspaces/adopt-pr",
        "awf workspace adopt-pr",
        "awf_adopt_pull_request_monitor",
    ):
        assert required in docs

    for unsupported in (
        "awf workspace adopt-pr-monitor",
        "awf adopt-pr",
        "POST /v1/workspaces/adopt-pr-monitor",
    ):
        assert unsupported not in docs


@pytest.mark.unit
def test_runbook_covers_auth_policy_idempotency_and_terminal_behavior() -> None:
    doc = _read("docs/PR_MONITOR_ADOPTION.md")

    for auth_term in (
        "AWF_API_TOKEN",
        "AWF_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "gh auth status",
    ):
        assert auth_term in doc

    for capability in (
        "PR metadata",
        "review threads",
        "checks",
        "push",
        "merge",
    ):
        assert capability.lower() in doc.lower()

    for policy_term in (
        "auto_merge=true",
        "--no-auto-merge",
        "initial_review_grace_period_seconds",
        "900",
    ):
        assert policy_term in doc

    for idempotency_term in (
        "deterministic",
        "repo/PR",
        "attached_existing",
        "PR_ADOPTION_POLICY_CONFLICT",
    ):
        assert idempotency_term in doc

    for terminal_retry_term in (
        "Terminal adoption retries",
        "destroyed",
        "destroying",
        "cancelled",
        "failed",
        "completed",
        "superseded",
        "fresh monitor",
        "attached_existing=false",
        "previous terminal adoption",
        "lineage",
    ):
        assert terminal_retry_term in doc

    for stale_terminal_phrase in (
        "terminal row can still satisfy",
        "may be returned as `attached_existing=true`",
        "Inspect the returned workspace status before assuming",
        "not landed",
    ):
        assert stale_terminal_phrase not in doc

    for terminal_status in (
        "destroyed",
        "destroying",
        "cancelled",
        "failed",
        "completed",
    ):
        assert terminal_status in doc

    for non_evergreen_phrase in (
        "does not start adoption today",
        "CLI currently exposes",
        "Current behavior",
    ):
        assert non_evergreen_phrase not in doc


@pytest.mark.unit
def test_runbook_documents_exact_grace_override_for_idempotent_retries() -> None:
    doc = _read("docs/PR_MONITOR_ADOPTION.md")

    for retry_term in (
        "Retry with the same raw grace override",
        "omitted/null",
        "explicit `900`",
    ):
        assert retry_term in doc
    assert re.search(r"different adoption\s+policies", doc)


@pytest.mark.unit
def test_runbook_documents_agent_default_idempotency_semantics() -> None:
    doc = _read("docs/PR_MONITOR_ADOPTION.md")
    normalized_doc = re.sub(r"\s+", " ", doc)

    for retry_term in (
        "omitting `agent` requests the default `codex` agent policy",
        "not a no-op",
        "conflicts with an existing live adoption for another agent",
        "omitting `model` or `effort` requests the default/no-override policy",
    ):
        assert retry_term in normalized_doc


@pytest.mark.unit
def test_runbook_covers_observability_and_recovery_surfaces() -> None:
    doc = _read("docs/PR_MONITOR_ADOPTION.md")

    for surface in (
        "console",
        "workspace details",
        "logs",
        "events",
        "validation provenance",
        "merge queue",
        "operations",
        "awf workspace show",
        "awf workspace events",
        "awf workspace operations",
        "awf workspace logs",
        "GET /v1/workspaces/{workspace_id}/validation",
        "GET /v1/merge-queue",
        "awf_list_workspace_validation",
        "awf_list_merge_queue",
        "awf_remonitor_workspace",
        "awf_request_workspace_validation",
        "awf_refresh_workspace",
        "awf_rebase_workspace",
        "awf_retry_workspace",
    ):
        assert surface in doc


@pytest.mark.unit
@pytest.mark.parametrize(
    ("doc_path", "heading"),
    (
        ("docs/PR_MONITOR_ADOPTION.md", "REST inspection:"),
        ("docs/REST_API_REFERENCE.md", "Inspect the adopted monitor:"),
    ),
)
def test_adoption_rest_inspection_examples_include_auth_header(doc_path: str, heading: str) -> None:
    doc = _read(doc_path)
    pattern = rf"{re.escape(heading)}\n\n.*?```bash\n(?P<block>.*?)\n```"
    match = re.search(pattern, doc, re.DOTALL)
    assert match is not None

    for line in match.group("block").splitlines():
        if line.startswith("curl "):
            assert '-H "Authorization: Bearer $AWF_API_TOKEN"' in line


@pytest.mark.unit
def test_runbook_provides_mocked_local_docs_tested_demo_path() -> None:
    doc = _read("docs/PR_MONITOR_ADOPTION.md")

    for proof in (
        "without mutating a live PR",
        "tests/unit/api/test_pr_monitor_adoption.py",
        "tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr",
        "tests/unit/mcp/test_mcp_server.py::TestToolRegistration::test_adopt_pull_request_monitor_tool_creates_adoption",
    ):
        assert proof in doc

    assert "uv run --python 3.12 --extra dev pytest" in doc
    assert "mocked" in doc.lower() or "no-live-pr" in doc.lower()


@pytest.mark.unit
def test_docs_do_not_require_caller_idempotency_key_or_expose_secret_literals() -> None:
    rest = _read("docs/REST_API_REFERENCE.md")
    adoption_section = rest.split("## PR Monitor Adoption", 1)[1]
    adoption_request_section = adoption_section.split("Inspect the adopted monitor:", 1)[0]

    assert "Requires `Idempotency-Key`" not in adoption_request_section
    assert '-H "Idempotency-Key' not in adoption_request_section
    assert "caller-supplied `Idempotency-Key`" not in adoption_request_section
    assert "AWF derives deterministic repo/PR idempotency" in adoption_request_section

    secret_patterns = (
        r"\bghp_[A-Za-z0-9_]{8,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{8,}\b",
        r"\bgh[osru]_[A-Za-z0-9_]{8,}\b",
    )
    for pattern in secret_patterns:
        for doc_path in ADOPTION_DOC_PATHS:
            assert not re.search(pattern, _read(doc_path)), (
                f"{doc_path} contains token-like literal: {pattern}"
            )


@pytest.mark.unit
def test_reference_docs_link_to_canonical_adoption_runbook() -> None:
    for relative_path in (
        "README.md",
        "docs/QUICKSTART.md",
        "docs/GETTING_STARTED.md",
        "docs/REST_API_REFERENCE.md",
        "docs/CLI_REFERENCE.md",
        "docs/MCP_REFERENCE.md",
        "docs/CLIENT_SURFACES.md",
    ):
        assert "PR_MONITOR_ADOPTION.md" in _read(relative_path), relative_path
