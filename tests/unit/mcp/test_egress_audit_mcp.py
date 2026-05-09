"""MCP tool tests for egress audit evidence.

TDD suite: these tests must fail before MCP egress audit tool lands.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_value
from awf.common.ids import new_egress_audit_record_id
from awf.db.enums import EgressDecision, WorkspaceStatus
from awf.db.models import EgressAuditRecord
from awf.db.repositories import EgressAuditRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server


@pytest.fixture
async def factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.fixture
def mcp(factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    service = WorkspaceService(factory)
    return build_mcp_server(service=service)


async def _workspace_with_audit(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/mcp-egress.git",
            branch_base="main",
            task_title="mcp egress audit",
            task_prompt="test mcp egress audit tool",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.running.value
        ws.resolved_profile = {
            "name": "test-profile",
            "version": 1,
            "security": {"egress": {"mode": "restricted"}},
        }
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        await session.flush()

        details = redact_audit_value(
            {
                "safe_field": "normal_value",
            }
        )

        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=ws.id,
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details=details,
        )
        session.add(record)
        await session.commit()
        return ws.id


@pytest.mark.unit
async def test_mcp_get_egress_audit_evidence_returns_record(
    factory: async_sessionmaker[AsyncSession],
    mcp,
) -> None:
    """MCP tool ``awf_get_egress_audit_evidence`` returns the latest record."""
    wid = await _workspace_with_audit(factory)

    result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": wid},
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is False


@pytest.mark.unit
async def test_mcp_get_egress_audit_evidence_no_record_returns_null(
    factory: async_sessionmaker[AsyncSession],
    mcp,
) -> None:
    """MCP tool returns a clear null indicator when no audit record exists."""
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/mcp-egress.git",
            branch_base="main",
            task_title="mcp egress no record",
            task_prompt="no audit evidence",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.running.value
        await session.commit()
        wid = ws.id

    result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": wid},
    )
    assert isinstance(result, CallToolResult)
    assert result.structuredContent is not None
    content = result.structuredContent
    assert content.get("evidence") is None


@pytest.mark.unit
async def test_mcp_get_egress_audit_evidence_lookup_error_returns_error(
    factory: async_sessionmaker[AsyncSession],
    mcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit lookup failures must not be flattened into null evidence."""
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/mcp-egress.git",
            branch_base="main",
            task_title="mcp egress lookup failure",
            task_prompt="audit lookup failure",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.running.value
        await session.commit()
        wid = ws.id

    async def _raise_lookup(
        _repo: EgressAuditRepository,
        _workspace_id: str,
    ) -> EgressAuditRecord | None:
        raise RuntimeError(
            "audit table unavailable at "
            "postgresql+asyncpg://awf:supersecret@db.internal:5432/awf "
            "Authorization: Bearer ghp_sensitiveToken123456"
        )

    monkeypatch.setattr(EgressAuditRepository, "get_latest_for_workspace", _raise_lookup)

    result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": wid},
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == "MCP_EGRESS_AUDIT_ERROR"
    message = result.structuredContent["message"]
    assert "supersecret" not in message
    assert "ghp_sensitiveToken123456" not in message
    assert "postgresql+asyncpg://[redacted]@db.internal:5432/awf" in message
    assert "Authorization: Bearer [redacted]" in message


@pytest.mark.unit
async def test_mcp_egress_audit_never_exposes_tokens(
    factory: async_sessionmaker[AsyncSession],
    mcp,
) -> None:
    """MCP egress audit evidence must never contain token patterns."""
    wid = await _workspace_with_audit(factory)

    result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": wid},
    )
    content_dump = json.dumps(result.model_dump(mode="json"), default=str)

    assert "ghp_" not in content_dump
    assert "sk-ant-" not in content_dump


@pytest.mark.unit
async def test_mcp_get_egress_audit_evidence_without_workspace_filter_returns_all_records(
    factory: async_sessionmaker[AsyncSession],
    mcp,
) -> None:
    """Omitted or blank ``workspace_id`` means list all audit evidence."""
    first_wid = await _workspace_with_audit(factory)
    second_wid = await _workspace_with_audit(factory)

    result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {},
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["workspace_id"] is None
    evidence = result.structuredContent["evidence"]
    assert isinstance(evidence, list)
    assert {record["workspace_id"] for record in evidence} == {first_wid, second_wid}

    blank_result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": "  "},
    )
    assert isinstance(blank_result, CallToolResult)
    assert blank_result.isError is False
    assert blank_result.structuredContent is not None
    blank_evidence = blank_result.structuredContent["evidence"]
    assert isinstance(blank_evidence, list)
    assert {record["workspace_id"] for record in blank_evidence} == {first_wid, second_wid}


@pytest.mark.unit
async def test_mcp_get_egress_audit_evidence_unknown_workspace_returns_null(
    mcp,
) -> None:
    """Unknown workspaces return the same null evidence envelope as missing audit rows."""
    result = await mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": "ws_missing"},
    )

    assert isinstance(result, CallToolResult)
    assert result.structuredContent == {"workspace_id": "ws_missing", "evidence": None}


class _FailingWorkspaceService:
    async def get_egress_audit_evidence(self, _workspace_id: str) -> None:
        raise RuntimeError(
            "database unavailable at "
            "postgresql+asyncpg://awf:supersecret@db.internal:5432/awf "
            "Authorization: Bearer ghp_sensitiveToken123456"
        )

    async def get(self, _workspace_id: str) -> None:
        raise RuntimeError(
            "database unavailable at "
            "postgresql+asyncpg://awf:supersecret@db.internal:5432/awf "
            "Authorization: Bearer ghp_sensitiveToken123456"
        )


@pytest.mark.unit
async def test_mcp_get_egress_audit_evidence_service_error_returns_error() -> None:
    """Service failures are returned as redacted MCP error results."""
    failing_mcp = build_mcp_server(service=_FailingWorkspaceService())  # type: ignore[arg-type]

    result = await failing_mcp.call_tool(
        "awf_get_egress_audit_evidence",
        {"workspace_id": "ws_error"},
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == "MCP_EGRESS_AUDIT_ERROR"
    message = result.structuredContent["message"]
    assert "supersecret" not in message
    assert "ghp_sensitiveToken123456" not in message
    assert "postgresql+asyncpg://[redacted]@db.internal:5432/awf" in message
    assert "Authorization: Bearer [redacted]" in message
