"""Shared fixtures and helpers for the AWF REST/CLI/MCP contract suite.

The contract suite asserts that the three supported client surfaces -- REST
(canonical), CLI, and MCP -- agree on request payloads, response payloads,
reason codes, idempotency keys, ``If-Match`` / workspace-version concurrency,
auth failures, and the structured error envelope. Tests live in this package
under ``tests/unit/contract`` so the existing per-surface suites in
``tests/unit/api``, ``tests/unit/cli``, and ``tests/unit/mcp`` keep their
ownership boundaries intact.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from awf.api.app import configure_database, create_app
from awf.api.schemas import ErrorResponse
from awf.cli.main import app as cli_app
from awf.common.config import Settings, get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.disk import DiskCheck


@dataclass
class ContractStack:
    """Bundle of REST, MCP, and CLI handles plus a shared session factory.

    All three surfaces are wired against the same per-test PostgreSQL schema and
    the same ``AWF_API_TOKEN`` value so the contract suite can call them
    interchangeably and compare the resulting payloads.
    """

    rest_client: AsyncClient
    mcp: Any
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    auth_headers: dict[str, str]
    cli_runner: CliRunner


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> AsyncIterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ok_disk_check(settings: Settings) -> DiskCheck:
    threshold = settings.min_free_disk_bytes
    free = threshold + 1
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=free,
        used_bytes=0,
        free_bytes=free,
        percent_free=100.0,
        threshold_bytes=threshold,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
    )


@pytest.fixture
async def contract_stack(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[ContractStack]:
    work_dir = tmp_path / "awf-state"
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    monkeypatch.setenv("AWF_MIN_FREE_DISK_BYTES", "700")
    get_settings.cache_clear()

    factory = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(work_dir),
        min_free_disk_bytes=700,
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_disk_check

    mcp = build_mcp_server(service=WorkspaceService(factory, settings=settings))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield ContractStack(
            rest_client=client,
            mcp=mcp,
            factory=factory,
            settings=settings,
            auth_headers={"Authorization": "Bearer secret"},
            cli_runner=CliRunner(),
        )


def unwrap_error_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize REST/MCP/CLI error envelopes to the canonical ``ErrorResponse`` shape.

    REST surfaces use two envelope flavors: ``HTTPException`` raises produce
    ``{"detail": {"error_code": ..., "message": ..., "detail": ...}}`` while
    routes that return ``JSONResponse(ErrorResponse(...).model_dump())`` produce
    a flat ``{"error_code": ..., "message": ..., "detail": ...}``. MCP tool
    errors return the flat shape directly. This helper accepts either input,
    validates the inner dict against ``ErrorResponse`` to confirm the contract,
    and returns its dump so callers compare a single canonical structure.
    """

    if "error_code" in payload:
        inner = payload
    elif "detail" in payload and isinstance(payload["detail"], dict):
        inner = payload["detail"]
    else:  # pragma: no cover - defensive: unrecognised envelope shapes flunk loud
        raise AssertionError(f"unrecognised error envelope: {payload!r}")
    return ErrorResponse.model_validate(inner).model_dump(mode="json")


async def call_mcp_structured(mcp: Any, name: str, args: dict[str, Any]) -> Any:
    """Invoke an MCP tool and return its ``structuredContent`` payload."""

    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


async def call_mcp_result(mcp: Any, name: str, args: dict[str, Any]) -> CallToolResult:
    """Invoke an MCP tool and return the raw ``CallToolResult`` for error cases."""

    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    return result


def invoke_cli(
    runner: CliRunner,
    args: list[str],
    *,
    response_status: int = 200,
    response_payload: object | None = None,
    response_text: str = "",
    env: dict[str, str] | None = None,
) -> tuple[Any, MagicMock]:
    """Invoke the AWF Typer CLI with ``httpx.request`` patched to a stub response.

    The contract suite uses this helper to assert the *outbound* HTTP request
    the CLI generates, not real backend behavior. Returns the Typer ``Result``
    and the captured ``httpx.request`` mock so callers can inspect call args.
    """

    response = MagicMock(spec=httpx.Response)
    response.status_code = response_status
    if response_payload is not None:
        response.content = json.dumps(response_payload).encode("utf-8")
        response.text = json.dumps(response_payload)
        response.json.return_value = response_payload
    elif response_text:
        response.content = response_text.encode("utf-8")
        response.text = response_text
        response.json.side_effect = ValueError("not json")
    else:
        response.content = b""
        response.text = ""
        response.json.return_value = None

    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = runner.invoke(cli_app, args, env=env)
    return result, mock


async def seed_monitoring_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str = "Contract monitoring workspace",
    final_status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    with_pr_url: bool = True,
    with_open_candidate: bool = False,
) -> str:
    """Seed a workspace walked through the canonical lifecycle.

    Mirrors the pattern in ``tests/unit/api/test_workspace_controls_idempotency``
    but is reimplemented locally so the contract suite owns a single seed shape
    and does not import test-private helpers from a sibling package.
    """

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/contract.git",
            branch_base="main",
            task_title=title,
            task_prompt=f"Implement {title}.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/awf/{workspace.id}/compose.yml"
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        if final_status == WorkspaceStatus.ready:
            await session.commit()
            return workspace.id
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.pushing, reason_code="SEED")
        if with_pr_url:
            workspace.pr_url = "https://github.com/example/contract/pull/42"
            workspace.pr_number = 42
            workspace.monitor_last_commit_sha = "b" * 40
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="SEED",
        )
        if final_status in {
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }:
            await repo.transition(workspace, to=final_status, reason_code="SEED")
        elif final_status != WorkspaceStatus.monitoring_pr:
            raise AssertionError(f"unsupported seed status {final_status}")

        if with_open_candidate:
            if not workspace.pr_url:
                raise AssertionError("open candidate seed requires a PR URL")
            await MergeCandidateRepository(session).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha=workspace.monitor_last_commit_sha,
                base_sha=workspace.base_commit,
            )

        await session.commit()
        return workspace.id


async def seed_requested_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str = "Contract requested workspace",
) -> str:
    """Seed a freshly-requested workspace (initial state, version=1)."""

    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/contract.git",
            branch_base="main",
            task_title=title,
            task_prompt=f"Implement {title}.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return workspace.id


async def seed_workspace_operation(
    factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    operation_type: OperationType = OperationType.cancel,
    status: OperationStatus = OperationStatus.succeeded,
) -> str:
    """Seed one operation row attached to ``workspace_id`` and return its id."""

    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=status,
            payload={"source": "contract_seed"},
        )
        await session.commit()
        return operation.id


def now_utc() -> datetime:
    return datetime.now(UTC)
