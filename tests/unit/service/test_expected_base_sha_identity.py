"""Durable create identity for the optional expected base SHA."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.repositories import WorkspaceRepository
from awf.service.workspaces import WorkspaceService
from awf.service.workspaces_create import (
    workspace_create_payload_matches,
    workspace_create_task_policy_snapshot,
)
from awf.service.workspaces_retry import _retry_task_policy
from tests.postgres import postgres_test_engine
from tests.unit.service.test_workspace_idempotency import _v2_request

_SHA = "a" * 40
_OTHER = "b" * 40


def _pinned(sha: str | None) -> WorkspaceCreateRequest:
    payload = _v2_request().model_dump(mode="python")
    if sha is None:
        payload["repo"].pop("expected_base_sha", None)
    else:
        payload["repo"]["expected_base_sha"] = sha
    return WorkspaceCreateRequest.model_validate(payload)


@pytest.mark.unit
def test_snapshot_stores_lowercase_pin_and_does_not_set_base_commit() -> None:
    policy = workspace_create_task_policy_snapshot(_pinned(_SHA.upper()))
    assert policy["expected_base_sha"] == _SHA
    assert "base_commit" not in policy
    omitted = workspace_create_task_policy_snapshot(_pinned(None))
    assert "expected_base_sha" not in omitted


@pytest.mark.unit
async def test_payload_match_treats_legacy_missing_pin_as_unpinned(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _pinned(None)
    created = await WorkspaceService(factory).create(
        request, idempotency_key="expected-base-legacy-unpinned"
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        assert workspace.base_commit is None
        assert "expected_base_sha" not in (workspace.task_policy or {})
        assert workspace_create_payload_matches(workspace, request)
        assert workspace_create_payload_matches(workspace, _pinned(_SHA)) is False


@pytest.mark.unit
async def test_payload_match_requires_the_same_normalized_pin(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _pinned(_SHA.upper())
    created = await WorkspaceService(factory).create(
        request, idempotency_key="expected-base-same-pin"
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        assert workspace.base_commit is None
        assert workspace.task_policy["expected_base_sha"] == _SHA
        assert workspace_create_payload_matches(workspace, _pinned(_SHA))
        assert workspace_create_payload_matches(workspace, _pinned(_SHA.upper()))
        assert workspace_create_payload_matches(workspace, _pinned(None)) is False
        assert workspace_create_payload_matches(workspace, _pinned(_OTHER)) is False


@pytest.mark.unit
def test_retry_task_policy_copies_pin_and_first_launch_evidence() -> None:
    evidence = {"sha": _SHA, "status": "verified"}
    source = SimpleNamespace(
        id="ws-pin-retry",
        agent="codex",
        failure_reason=None,
        task_policy={
            "expected_base_sha": _SHA,
            "expected_base_sha_first_launch": evidence,
        },
    )
    policy, _agent = _retry_task_policy(source, (), planning_scope_context=None)
    assert policy["expected_base_sha"] == _SHA
    assert policy["expected_base_sha_first_launch"] == evidence
    policy["expected_base_sha_first_launch"]["status"] = "mutated"
    assert source.task_policy["expected_base_sha_first_launch"]["status"] == "verified"


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    async with postgres_test_engine() as engine:
        from awf.db.session import make_session_factory

        yield make_session_factory(engine)
