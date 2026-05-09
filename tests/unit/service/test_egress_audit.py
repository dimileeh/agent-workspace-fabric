"""Egress audit evidence service tests.

TDD suite: these tests must fail before any egress audit implementation lands.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.audit import REDACTION_MARKER
from awf.common.ids import new_egress_audit_record_id
from awf.db.enums import EgressDecision, WorkspaceStatus
from awf.db.models import EgressAuditRecord
from awf.db.repositories import EgressAuditRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import WorkspaceService


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _workspace(
    sf: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus = WorkspaceStatus.ready,
    network_posture: str = "restricted",
) -> str:
    async with sf() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/egress.git",
            branch_base="main",
            task_title="egress audit",
            task_prompt="test egress audit evidence",
            agent="codex",
            test_commands=[],
        )
        ws.status = status.value
        ws.resolved_profile = {
            "name": "test-profile",
            "version": 1,
            "security": {"egress": {"mode": network_posture}},
        }
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        await session.commit()
        return ws.id


# --- Phase 1: Core audit record tests ---


@pytest.mark.unit
async def test_record_egress_audit_persists_all_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ``EgressAuditRecord`` must store all contract fields."""
    wid = await _workspace(session_factory, network_posture="open")

    async with session_factory() as session:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            attempt_id=None,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={"internet_access": "unrestricted"},
        )
        session.add(record)
        await session.flush()

        assert record.id is not None
        assert record.workspace_id == wid
        assert record.attempt_id is None
        assert record.policy_posture == "open"
        assert record.decision == "allow"
        assert record.destination_category == "public_internet"
        assert record.reason_code == "LOCAL_EGRESS_OPEN_UNRESTRICTED"
        assert record.details == {"internet_access": "unrestricted"}
        assert record.enforced_at is not None
        assert record.created_at is not None


@pytest.mark.unit
async def test_record_egress_audit_open_mode_allow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``open`` posture maps to ``decision=allow`` with ``public_internet`` category."""
    wid = await _workspace(session_factory, network_posture="open")

    async with session_factory() as session:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={"internet_access": "unrestricted"},
        )
        session.add(record)
        await session.flush()

        assert record.decision == "allow"
        assert record.destination_category == "public_internet"
        assert record.reason_code == "LOCAL_EGRESS_OPEN_UNRESTRICTED"


@pytest.mark.unit
async def test_record_egress_audit_offline_mode_deny(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``offline`` posture maps to ``deny`` with ``internal_only`` category."""
    wid = await _workspace(session_factory, network_posture="offline")

    async with session_factory() as session:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="offline",
            decision=EgressDecision.deny.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_OFFLINE_NETWORK",
            details={"internet_access": "disabled"},
        )
        session.add(record)
        await session.flush()

        assert record.decision == "deny"
        assert record.destination_category == "internal_only"
        assert record.reason_code == "LOCAL_EGRESS_OFFLINE_NETWORK"


@pytest.mark.unit
async def test_record_egress_audit_restricted_mode_deferred(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``restricted`` posture maps to ``deferred`` with ``internal_only`` category."""
    wid = await _workspace(session_factory, network_posture="restricted")

    async with session_factory() as session:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details={
                "internet_access": "internal_only",
                "destination_filtering": "deferred",
                "allowlist_templates": ["github", "pypi"],
            },
        )
        session.add(record)
        await session.flush()

        assert record.decision == "deferred"
        assert record.destination_category == "internal_only"
        assert record.reason_code == "LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY"
        assert record.details.get("destination_filtering") == "deferred"


@pytest.mark.unit
async def test_egress_audit_redacts_token_patterns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Secrets in details JSONB must be redacted before persistence."""
    wid = await _workspace(session_factory)

    async with session_factory() as session:
        from awf.common.audit import redact_audit_value

        sensitive_details = {
            "token": "ghp_secret123456789012345678",
            "api_key": "sk-ant-something-secret-with-manychars",
            "nested": {"github_pat_topsecret2024abcdefgh"},
            "safe_field": "normal_value",
        }
        redacted = redact_audit_value(sensitive_details)

        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details=redacted,
        )
        session.add(record)
        await session.flush()

        stored = json.dumps(record.details)
        assert "ghp_" not in stored
        assert "sk-ant-" not in stored
        assert "github_pat_" not in stored
        assert REDACTION_MARKER in stored
        assert record.details["safe_field"] == "normal_value"


@pytest.mark.unit
async def test_egress_audit_details_include_allowlist_templates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Restricted mode details must carry allowlist template information."""
    wid = await _workspace(session_factory, network_posture="restricted")

    async with session_factory() as session:
        templates = ["github", "pypi", "npm"]
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details={
                "destination_filtering": "deferred",
                "allowlist_templates": templates,
            },
        )
        session.add(record)
        await session.flush()

        assert record.details.get("allowlist_templates") == templates


@pytest.mark.unit
async def test_get_latest_audit_for_workspace_returns_newest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Latest audit query must return the most recent record for a workspace."""
    wid = await _workspace(session_factory, network_posture="open")

    async with session_factory() as session:
        older = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={"iteration": 1},
        )
        older.enforced_at = datetime(2024, 1, 1, tzinfo=UTC)
        session.add(older)

        newer = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={"iteration": 2},
        )
        newer.enforced_at = datetime(2025, 6, 1, tzinfo=UTC)
        session.add(newer)
        await session.flush()

        repo = EgressAuditRepository(session)
        latest = await repo.get_latest_for_workspace(wid)

        assert latest is not None
        assert latest.details == {"iteration": 2}


@pytest.mark.unit
async def test_get_summary_counts_by_posture(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Summary query must group records by posture with correct counts."""

    async with session_factory() as session:
        r1 = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=await _workspace(session_factory, network_posture="open"),
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={},
        )
        session.add(r1)

        r2 = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=await _workspace(session_factory, network_posture="restricted"),
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details={},
        )
        session.add(r2)

        r3 = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=await _workspace(session_factory, network_posture="offline"),
            policy_posture="offline",
            decision=EgressDecision.deny.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_OFFLINE_NETWORK",
            details={},
        )
        session.add(r3)
        await session.flush()

        repo = EgressAuditRepository(session)
        counts = await repo.summary_counts_by_posture()

        assert counts.get("open", 0) == 1
        assert counts.get("restricted", 0) == 1
        assert counts.get("offline", 0) == 1


@pytest.mark.unit
async def test_get_summary_counts_by_posture_excludes_terminal_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Summary query must exclude records from terminal-workspace statuses."""

    async with session_factory() as session:
        r_active = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=await _workspace(session_factory, network_posture="open"),
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={},
        )
        session.add(r_active)

        destroyed_wid = await _workspace(session_factory, status=WorkspaceStatus.destroyed)
        r_destroyed = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=destroyed_wid,
            policy_posture="restricted",
            decision=EgressDecision.deny.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_DENIED",
            details={},
        )
        session.add(r_destroyed)
        await session.flush()

        repo = EgressAuditRepository(session)
        counts = await repo.summary_counts_by_posture()

        assert counts.get("open", 0) == 1
        assert counts.get("restricted", 0) == 0
        assert "restricted" not in counts or counts["restricted"] == 0


@pytest.mark.unit
async def test_summary_counts_by_posture_filters_active_workspaces_before_ranking(
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    """The latest-audit ranking should only scan current workspaces."""

    async with session_factory() as session:
        active = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=await _workspace(session_factory, network_posture="open"),
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={},
        )
        session.add(active)

        destroyed = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=await _workspace(session_factory, status=WorkspaceStatus.destroyed),
            policy_posture="restricted",
            decision=EgressDecision.deny.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_DENIED",
            details={},
        )
        session.add(destroyed)
        await session.flush()

        captured_sql: list[str] = []

        def _capture_statement(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if "latest_ranked" in statement:
                captured_sql.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", _capture_statement)
        try:
            counts = await EgressAuditRepository(session).summary_counts_by_posture()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture_statement)

    assert counts == {"open": 1}
    assert captured_sql
    ranked_subquery_sql = captured_sql[-1].split(") as latest_ranked", 1)[0]
    assert "join workspaces" in ranked_subquery_sql
    assert "workspaces.status not in" in ranked_subquery_sql


@pytest.mark.unit
async def test_summary_counts_by_posture_uses_latest_audit_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A workspace with multiple audit records must contribute only once, using the latest."""
    async with session_factory() as session:
        wid_retried = await _workspace(session_factory, network_posture="restricted")
        r_old = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid_retried,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={},
            enforced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        session.add(r_old)

        r_new = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid_retried,
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details={},
            enforced_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        session.add(r_new)

        wid_single = await _workspace(session_factory, network_posture="open")
        r_single = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid_single,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={},
        )
        session.add(r_single)
        await session.flush()

        repo = EgressAuditRepository(session)
        counts = await repo.summary_counts_by_posture()

        assert counts.get("restricted", 0) == 1
        assert counts.get("open", 0) == 1


@pytest.mark.unit
async def test_workspace_response_includes_egress_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``WorkspaceResponse`` must include the latest egress audit evidence."""
    wid = await _workspace(session_factory, network_posture="open")

    async with session_factory() as session:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="open",
            decision=EgressDecision.allow.value,
            destination_category="public_internet",
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={"internet_access": "unrestricted"},
        )
        session.add(record)
        await session.commit()

    detail = await WorkspaceService(session_factory).get(wid)

    assert detail is not None
    egress_audit = detail.model_dump().get("egress_audit")
    assert egress_audit is not None
    assert egress_audit["policy_posture"] == "open"
    assert egress_audit["decision"] == "allow"
    assert egress_audit["destination_category"] == "public_internet"
    assert egress_audit["reason_code"] == "LOCAL_EGRESS_OPEN_UNRESTRICTED"


@pytest.mark.unit
async def test_workspace_response_redacts_untrusted_egress_audit_details(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``WorkspaceResponse`` must not trust stored audit details to be pre-redacted."""
    wid = await _workspace(session_factory, network_posture="restricted")

    async with session_factory() as session:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=wid,
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="allowlisted_public_internet",
            reason_code="LOCAL_EGRESS_RESTRICTED_ALLOWLISTED",
            details={
                "safe_field": "normal_value",
                "token": "ghp_secret123456789012345678",
                "nested": {
                    "api_key": "sk-ant-something-secret-with-manychars",
                    "url": "https://user:password123456@github.com/org/repo",
                    "usage": {"total_tokens": 42},
                },
            },
        )
        session.add(record)
        await session.commit()

    detail = await WorkspaceService(session_factory).get(wid)

    assert detail is not None
    egress_audit = detail.model_dump().get("egress_audit")
    assert egress_audit is not None
    response_details = egress_audit["details"]
    serialized = json.dumps(response_details)
    assert response_details["safe_field"] == "normal_value"
    assert response_details["token"] == REDACTION_MARKER
    assert response_details["nested"]["api_key"] == REDACTION_MARKER
    assert response_details["nested"]["usage"]["total_tokens"] == 42
    assert "ghp_" not in serialized
    assert "sk-ant-" not in serialized
    assert "password123456" not in serialized


@pytest.mark.unit
async def test_workspace_response_egress_audit_null_when_no_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``WorkspaceResponse.egress_audit`` must be ``None`` when no audit exists."""
    wid = await _workspace(session_factory)

    detail = await WorkspaceService(session_factory).get(wid)

    assert detail is not None
    assert detail.model_dump().get("egress_audit") is None


@pytest.mark.unit
async def test_workspace_response_egress_audit_null_when_lookup_fails(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit lookup failures must not block the workspace detail response."""
    wid = await _workspace(session_factory)

    async def _raise_lookup(
        _repo: EgressAuditRepository,
        _workspace_id: str,
    ) -> EgressAuditRecord | None:
        raise RuntimeError("audit lookup failed")

    monkeypatch.setattr(EgressAuditRepository, "get_latest_for_workspace", _raise_lookup)

    detail = await WorkspaceService(session_factory).get(wid)

    assert detail is not None
    assert detail.model_dump().get("egress_audit") is None
