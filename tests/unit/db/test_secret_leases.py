"""Workspace secret lease repository tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent, WorkspaceSecretLease
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceRepository,
    _secret_lease_insert_if_absent_stmt,
)
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _workspace(session: AsyncSession) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/secrets.git",
        branch_base="main",
        task_title="secret lease test",
        task_prompt="exercise secret lease metadata",
        agent="codex",
        test_commands=[],
    )
    workspace.status = WorkspaceStatus.provisioning.value
    await session.flush()
    return workspace


def _lease_issues(now: datetime) -> list[SecretLeaseIssue]:
    expires_at = now + timedelta(hours=1)
    return [
        SecretLeaseIssue(
            secret_name="api-token",
            kind="env",
            target="API_TOKEN",
            mode="ro",
            required=True,
            provider="env",
            ref_digest="sha256:" + "a" * 64,
            expires_at=expires_at,
            issue_metadata={"profile": "secure-local", "declaration_index": 0},
        ),
        SecretLeaseIssue(
            secret_name="db-password",
            kind="mount",
            target="/run/awf/secrets/db-password",
            mode="ro",
            required=False,
            provider="vault",
            ref_digest="sha256:" + "b" * 64,
            expires_at=expires_at,
            issue_metadata={"profile": "secure-local", "declaration_index": 1},
        ),
    ]


@pytest.mark.unit
def test_secret_lease_issue_insert_has_conflict_guard() -> None:
    stmt = _secret_lease_insert_if_absent_stmt("postgresql")

    assert stmt is not None
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (workspace_id, secret_name, kind, target) DO NOTHING" in sql
    assert "RETURNING" in sql


@pytest.mark.unit
def test_secret_lease_issue_insert_unsupported_dialect_has_no_conflict_guard() -> None:
    assert _secret_lease_insert_if_absent_stmt("mysql") is None


async def _events(session: AsyncSession, workspace_id: str) -> list[WorkspaceEvent]:
    rows = await session.execute(
        select(WorkspaceEvent)
        .where(WorkspaceEvent.workspace_id == workspace_id)
        .order_by(WorkspaceEvent.occurred_at, WorkspaceEvent.id)
    )
    return list(rows.scalars())


@pytest.mark.unit
async def test_issue_profile_secret_leases_persists_sanitized_metadata(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    leases = await SecretLeaseRepository(session).issue_declared_leases(
        workspace,
        leases=_lease_issues(now),
        now=now,
    )

    assert [lease.status for lease in leases] == ["issued", "issued"]
    assert {lease.secret_name for lease in leases} == {"api-token", "db-password"}
    assert leases[0].workspace_id == workspace.id
    assert leases[0].attempt_id is None
    assert leases[0].issued_at == now
    assert leases[0].expires_at == now + timedelta(hours=1)
    assert leases[0].kind == "env"
    assert leases[0].target == "API_TOKEN"
    assert leases[0].mode == "ro"
    assert leases[0].required is True
    assert leases[0].provider == "env"
    assert leases[0].ref_digest == "sha256:" + "a" * 64
    assert leases[0].issue_metadata["profile"] == "secure-local"
    assert "secret-value" not in json.dumps([lease.__dict__ for lease in leases], default=str)


@pytest.mark.unit
async def test_issue_declared_leases_handles_empty_declarations(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)

    leases = await SecretLeaseRepository(session).issue_declared_leases(
        workspace,
        leases=[],
        now=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
    )

    assert leases == []
    assert "SECRET_LEASE_ISSUED" not in [
        event.reason_code for event in await _events(session, workspace.id)
    ]


@pytest.mark.unit
async def test_issue_declared_leases_falls_back_without_conflict_helper(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session, dialect_name="unsupported")

    leases = await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest="sha256:" + "a" * 64,
                expires_at=now + timedelta(hours=1),
                issue_metadata={"profile": "secure-local", "declaration_index": 0},
            )
        ],
        now=now,
    )

    assert len(leases) == 1
    assert leases[0].secret_name == "api-token"
    assert leases[0].issued_at == now


@pytest.mark.unit
async def test_issue_declared_leases_fallback_accepts_optional_metadata(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session, dialect_name="unsupported")

    leases = await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="fallback-token",
                kind="env",
                target="FALLBACK_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest=None,
                expires_at=None,
                issue_metadata={},
            )
        ],
        now=now,
    )

    assert len(leases) == 1
    assert leases[0].secret_name == "fallback-token"
    assert leases[0].issued_at == now
    assert leases[0].ref_digest is None
    assert leases[0].expires_at is None


@pytest.mark.unit
async def test_issue_declared_leases_is_idempotent_for_workspace_profile_retry(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session)

    first = await repo.issue_declared_leases(workspace, leases=_lease_issues(now), now=now)
    second = await repo.issue_declared_leases(
        workspace,
        leases=_lease_issues(now + timedelta(minutes=5)),
        now=now + timedelta(minutes=5),
    )

    assert [lease.id for lease in second] == [lease.id for lease in first]
    rows = await repo.list_for_workspace(workspace.id)
    assert len(rows) == 2
    assert {lease.status for lease in rows} == {"issued"}


@pytest.mark.unit
async def test_issue_declared_leases_fetches_existing_workspace_rows_once(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session)
    await repo.issue_declared_leases(workspace, leases=_lease_issues(now), now=now)

    statements: list[str] = []
    bind = session.get_bind()

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(bind, "before_cursor_execute", record_sql)
    try:
        await repo.issue_declared_leases(
            workspace,
            leases=_lease_issues(now + timedelta(minutes=5)),
            now=now + timedelta(minutes=5),
        )
    finally:
        event.remove(bind, "before_cursor_execute", record_sql)

    lease_selects = [
        statement
        for statement in statements
        if statement.startswith("select") and "from workspace_secret_leases" in statement
    ]
    assert len(lease_selects) == 1
    assert "where workspace_secret_leases.workspace_id =" in lease_selects[0]


@pytest.mark.unit
async def test_issue_declared_leases_reissues_revoked_rows_with_fresh_metadata(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    retry_now = now + timedelta(minutes=5)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session)
    first = await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest="sha256:" + "a" * 64,
                expires_at=now + timedelta(hours=1),
                issue_metadata={"profile": "secure-local", "declaration_index": 0},
            )
        ],
        now=now,
    )
    await repo.revoke_workspace_leases(
        workspace,
        now=now + timedelta(minutes=1),
        reason_code="PROVISIONING_FAILED",
    )

    retried = await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="rw",
                required=False,
                provider="vault",
                ref_digest="sha256:" + "c" * 64,
                expires_at=retry_now + timedelta(hours=2),
                issue_metadata={"profile": "secure-local-v2", "declaration_index": 0},
            )
        ],
        now=retry_now,
    )

    assert [lease.id for lease in retried] == [first[0].id]
    lease = retried[0]
    assert lease.status == "issued"
    assert lease.issued_at == retry_now
    assert lease.expires_at == retry_now + timedelta(hours=2)
    assert lease.mounted_at is None
    assert lease.revoked_at is None
    assert lease.revoke_reason_code is None
    assert lease.mode == "rw"
    assert lease.required is False
    assert lease.provider == "vault"
    assert lease.ref_digest == "sha256:" + "c" * 64
    assert lease.issue_metadata["profile"] == "secure-local-v2"

    mounted = await repo.mark_issued_mounted(
        workspace,
        now=retry_now + timedelta(minutes=1),
    )
    assert mounted == [lease]
    assert lease.status == "mounted"

    events = await _events(session, workspace.id)
    reason_codes = [event.reason_code for event in events]
    assert reason_codes.count("SECRET_LEASE_ISSUED") == 2
    assert reason_codes.count("SECRET_LEASE_REVOKED") == 1


@pytest.mark.unit
async def test_issue_declared_leases_reissues_active_rows_when_declaration_changes(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    retry_now = now + timedelta(minutes=5)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session)
    first = await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest="sha256:" + "a" * 64,
                expires_at=now + timedelta(hours=1),
                issue_metadata={"profile": "secure-local", "declaration_index": 0},
            )
        ],
        now=now,
    )

    retried = await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="vault",
                ref_digest="sha256:" + "d" * 64,
                expires_at=retry_now + timedelta(hours=1),
                issue_metadata={"profile": "secure-local", "declaration_index": 0},
            )
        ],
        now=retry_now,
    )

    assert [lease.id for lease in retried] == [first[0].id]
    lease = retried[0]
    assert lease.status == "issued"
    assert lease.issued_at == retry_now
    assert lease.provider == "vault"
    assert lease.ref_digest == "sha256:" + "d" * 64

    events = await _events(session, workspace.id)
    assert [event.reason_code for event in events].count("SECRET_LEASE_ISSUED") == 2


@pytest.mark.unit
async def test_mount_expire_revoke_and_audit_events_are_recorded(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session)
    await repo.issue_declared_leases(workspace, leases=_lease_issues(now), now=now)

    mounted = await repo.mark_issued_mounted(
        workspace,
        now=now + timedelta(minutes=1),
        mount_metadata={
            "mount_plan": "local-profile-declarations",
            "compose_project": "awf_ws_secret",
            "secret_value": "must-not-persist",
        },
    )
    assert {lease.status for lease in mounted} == {"mounted"}
    assert all(lease.mounted_at == now + timedelta(minutes=1) for lease in mounted)
    assert "must-not-persist" not in json.dumps(
        [lease.mount_metadata for lease in mounted],
        default=str,
    )

    expired = await repo.expire_due_leases(now=now + timedelta(hours=2))
    assert {lease.status for lease in expired} == {"expired"}

    revoked = await repo.revoke_workspace_leases(
        workspace,
        now=now + timedelta(hours=3),
        reason_code="TERMINAL_CLEANUP",
    )
    assert {lease.status for lease in revoked} == {"revoked"}
    assert all(lease.revoke_reason_code == "TERMINAL_CLEANUP" for lease in revoked)
    assert all(lease.revoked_at == now + timedelta(hours=3) for lease in revoked)

    replay = await repo.revoke_workspace_leases(
        workspace,
        now=now + timedelta(hours=4),
        reason_code="TERMINAL_CLEANUP",
    )
    assert replay == []
    assert {lease.revoked_at for lease in revoked} == {now + timedelta(hours=3)}

    events = await _events(session, workspace.id)
    reason_codes = [event.reason_code for event in events]
    assert reason_codes.count("SECRET_LEASE_ISSUED") == 2
    assert reason_codes.count("SECRET_LEASE_MOUNTED") == 2
    assert reason_codes.count("SECRET_LEASE_EXPIRED") == 2
    assert reason_codes.count("SECRET_LEASE_REVOKED") == 2
    payloads = [event.payload for event in events if event.event_type == "workspace.secret_lease"]
    assert all(payload and payload["schema"] == "secret_lease_audit.v1" for payload in payloads)
    assert "must-not-persist" not in json.dumps(payloads, default=str)


@pytest.mark.unit
async def test_expire_due_leases_only_changes_due_active_rows(session: AsyncSession) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    other = await _workspace(session)
    repo = SecretLeaseRepository(session)
    due = _lease_issues(now)
    future = [
        SecretLeaseIssue(
            secret_name="future-token",
            kind="env",
            target="FUTURE_TOKEN",
            mode="ro",
            required=True,
            provider="env",
            ref_digest="sha256:" + "c" * 64,
            expires_at=now + timedelta(days=1),
            issue_metadata={"profile": "secure-local", "declaration_index": 0},
        )
    ]
    await repo.issue_declared_leases(workspace, leases=due, now=now)
    await repo.issue_declared_leases(other, leases=future, now=now)

    expired = await repo.expire_due_leases(now=now + timedelta(hours=2))

    assert {lease.workspace_id for lease in expired} == {workspace.id}
    assert {lease.status for lease in expired} == {"expired"}
    rows = await session.execute(select(WorkspaceSecretLease))
    statuses = {lease.secret_name: lease.status for lease in rows.scalars()}
    assert statuses == {
        "api-token": "expired",
        "db-password": "expired",
        "future-token": "issued",
    }


@pytest.mark.unit
async def test_issue_event_omits_empty_issue_metadata(session: AsyncSession) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    repo = SecretLeaseRepository(session)

    await repo.issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="metadata-free-token",
                kind="env",
                target="METADATA_FREE_TOKEN",
                mode="ro",
                required=True,
                provider=None,
                ref_digest=None,
                expires_at=None,
                issue_metadata={},
            )
        ],
        now=now,
    )

    events = await _events(session, workspace.id)
    payload = events[-1].payload
    assert payload is not None
    assert payload["reason_code"] == "SECRET_LEASE_ISSUED"
    assert "issue_metadata" not in payload
    assert await repo._workspaces_by_id(set()) == {}


@pytest.mark.unit
async def test_expire_due_leases_records_workspace_event_for_valid_rows(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    lease = WorkspaceSecretLease(
        id="sl_expiring_test_lease",
        workspace_id=workspace.id,
        secret_name="expiring-token",
        kind="env",
        target="EXPIRING_TOKEN",
        mode="ro",
        required=True,
        provider=None,
        ref_digest=None,
        status="issued",
        issued_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        issue_metadata={},
        mount_metadata={},
    )
    session.add(lease)
    await session.flush()

    expired = await SecretLeaseRepository(session).expire_due_leases(now=now)

    assert expired == [lease]
    assert lease.status == "expired"
    events = (await session.execute(select(WorkspaceEvent))).scalars().all()
    assert events[-1].event_type == "workspace.secret_lease"
    assert events[-1].reason_code == "SECRET_LEASE_EXPIRED"
