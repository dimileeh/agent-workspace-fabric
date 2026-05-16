"""Secret lease service tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent, WorkspaceSecretLease
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import ProfileSecret, WorkspaceProfile
from awf.service.secret_leases import SecretLeaseService
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _workspace(session: AsyncSession) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/secret-service.git",
        branch_base="main",
        task_title="secret service",
        task_prompt="exercise secret service",
        agent="codex",
        test_commands=[],
    )
    workspace.status = WorkspaceStatus.provisioning.value
    await session.flush()
    return workspace


def _profile(*, raw_ref: str = "env/API_TOKEN") -> WorkspaceProfile:
    return WorkspaceProfile(
        name="local-secrets",
        secrets=[
            ProfileSecret(
                name="api-token",
                kind="env",
                target="API_TOKEN",
                provider="env",
                ref=raw_ref,
            ),
            ProfileSecret(
                name="ssh-key",
                kind="mount",
                target="/run/awf/secrets/ssh-key",
                required=False,
                provider="local-file",
                ref="host/ssh-key",
            ),
        ],
    )


@pytest.mark.unit
async def test_issue_profile_secret_leases_sanitizes_profile_declarations(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 11, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    service = SecretLeaseService(session)

    leases = await service.issue_profile_secret_leases(
        workspace,
        _profile(),
        now=now,
        ttl_seconds=900,
    )

    assert [lease.secret_name for lease in leases] == ["api-token", "ssh-key"]
    assert leases[0].kind == "env"
    assert leases[0].target == "API_TOKEN"
    assert leases[0].provider == "env"
    assert leases[0].ref_digest.startswith("sha256:")
    assert leases[0].expires_at == now + timedelta(seconds=900)
    assert leases[0].issue_metadata == {
        "schema": "secret_lease_issue_metadata.v1",
        "profile_name": "local-secrets",
        "profile_source": "inline",
        "declaration_index": 0,
    }


@pytest.mark.unit
async def test_issue_profile_secret_leases_noops_for_profile_without_secrets(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    service = SecretLeaseService(session)
    profile = WorkspaceProfile(name="no-secrets", secrets=[])

    leases = await service.issue_profile_secret_leases(workspace, profile)

    assert leases == []
    assert await service.workspace_secret_lease_status(workspace.id) == []


@pytest.mark.unit
async def test_optional_ref_naive_now_and_status_sort_fallback(
    session: AsyncSession,
) -> None:
    naive_now = datetime(2026, 4, 29, 11, 0)
    workspace = await _workspace(session)
    service = SecretLeaseService(session)
    profile = WorkspaceProfile(
        name="optional-ref",
        secrets=[
            ProfileSecret(
                name="optional-token",
                kind="env",
                target="OPTIONAL_TOKEN",
                provider=None,
                ref=None,
            )
        ],
    )

    leases = await service.issue_profile_secret_leases(
        workspace,
        profile,
        now=naive_now,
        ttl_seconds=60,
    )
    leases[0].issue_metadata = {"declaration_index": "not-an-int"}
    await session.flush()

    status = await service.workspace_secret_lease_status(workspace.id)

    assert leases[0].ref_digest is None
    assert leases[0].issued_at == naive_now.replace(tzinfo=UTC)
    assert status[0].issued_at == naive_now.replace(tzinfo=UTC)
    assert status[0].ref_digest is None


@pytest.mark.unit
async def test_secret_values_do_not_appear_in_rows_events_status_or_repr(
    session: AsyncSession,
) -> None:
    raw_secret = "sk-live-do-not-store-service-secret"
    now = datetime(2026, 4, 29, 11, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    service = SecretLeaseService(session)

    await service.issue_profile_secret_leases(
        workspace,
        _profile(raw_ref=raw_secret),
        now=now,
        ttl_seconds=900,
    )
    await service.record_secret_lease_mounts(
        workspace,
        now=now + timedelta(seconds=1),
        mount_metadata={"path": "/run/awf/secrets", "token": raw_secret},
    )

    status = await service.workspace_secret_lease_status(workspace.id)
    rows = (await session.execute(select(WorkspaceSecretLease))).scalars().all()
    events = (await session.execute(select(WorkspaceEvent))).scalars().all()
    rendered = json.dumps(
        {
            "rows": [
                {
                    "id": row.id,
                    "secret_name": row.secret_name,
                    "kind": row.kind,
                    "target": row.target,
                    "provider": row.provider,
                    "ref_digest": row.ref_digest,
                    "status": row.status,
                    "issue_metadata": row.issue_metadata,
                    "mount_metadata": row.mount_metadata,
                    "repr": repr(row),
                }
                for row in rows
            ],
            "events": [event.payload for event in events],
            "status": [item.model_dump(mode="json") for item in status],
        },
        default=str,
    )

    assert raw_secret not in rendered
    assert "[redacted]" in rendered


@pytest.mark.unit
async def test_workspace_secret_lease_status_exposes_non_secret_fields(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 4, 29, 11, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    service = SecretLeaseService(session)
    await service.issue_profile_secret_leases(workspace, _profile(), now=now)

    status = await service.workspace_secret_lease_status(workspace.id)

    assert len(status) == 2
    assert status[0].lease_id.startswith("sl_")
    assert status[0].secret_name == "api-token"
    assert status[0].kind == "env"
    assert status[0].target == "API_TOKEN"
    assert status[0].provider == "env"
    assert status[0].ref_digest.startswith("sha256:")
    assert status[0].status == "issued"
    assert status[0].issued_at == now
    assert "env/API_TOKEN" not in json.dumps(
        [item.model_dump(mode="json") for item in status],
        default=str,
    )


@pytest.mark.unit
async def test_expiry_uses_injected_now_without_sleeping(session: AsyncSession) -> None:
    now = datetime(2026, 4, 29, 11, 0, tzinfo=UTC)
    workspace = await _workspace(session)
    service = SecretLeaseService(session)
    await service.issue_profile_secret_leases(
        workspace,
        _profile(),
        now=now,
        ttl_seconds=5,
    )

    early = await service.expire_due_secret_leases(now=now + timedelta(seconds=4))
    due = await service.expire_due_secret_leases(now=now + timedelta(seconds=5))

    assert early == []
    assert {lease.status for lease in due} == {"expired"}
