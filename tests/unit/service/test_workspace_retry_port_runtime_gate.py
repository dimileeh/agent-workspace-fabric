"""Runtime-gate override and legacy hostname retry port tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import ResourceReservationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import create_workspace_row, retry_workspace_row
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a disposable test database."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request(
    *,
    task_kind: str = "feature_branch_pr",
    provider_readiness_override: bool = True,
) -> WorkspaceCreateRequest:
    payload: dict[str, object] = {
        "repo": {"url": "git@github.com:example/retryable.git", "base_branch": "development"},
        "task": {
            "title": "Retry flaky validation",
            "prompt": "Fix the intermittent validation failure.",
            "agent": "codex",
            "kind": task_kind,
            "external_id": "TICKET-RETRY",
            "task_class": "test_task",
            "owned_paths": ["src/awf/retry/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 30,
        },
        "workspace": {"profile_ref": "python", "profile": None},
        "validation": {"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        "resources": {},
    }
    if provider_readiness_override:
        payload["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "retry service test fixture",
        }
    return WorkspaceCreateRequest.model_validate(payload)


def _request_with_preflight_override(
    *,
    reason: str = "operator verified provider readiness manually",
) -> WorkspaceCreateRequest:
    payload = _request().model_dump(mode="json")
    payload["preflight"] = {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": reason,
    }
    return WorkspaceCreateRequest.model_validate(payload)


def _settings_with_host_home(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
    )


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    branch_name: str = "codex/old-attempt",
    remote_push_branch: str | None = None,
    release_runtime: bool = True,
) -> dict[str, object]:
    """Mark a workspace as failed with shared transition/evidence payload."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.pr_url = None
        workspace.compose_project_name = "awf_old_attempt"
        assert workspace.resolved_profile is not None
        frozen_profile = {
            **workspace.resolved_profile,
            "source": "frozen:test-profile",
        }
        workspace.resolved_profile = frozen_profile
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        if release_runtime:
            await repo.add_event(
                workspace,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
            )
        await session.commit()
        return frozen_profile


@pytest.mark.unit
async def test_retry_runtime_gate_override_excludes_source_from_port_conflict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When ignore_source_runtime_check=True, the runtime-not-released gate
    is skipped, and the source workspace is excluded from port conflict
    scanning because the retry replaces the source. Even though the source
    still holds host ports (no runtime release event), retry succeeds because
    excluding the source avoids a false conflict with itself."""
    settings = _settings_with_host_home(tmp_path)
    req = _request_with_preflight_override()
    companion_req = {
        "name": "sidecar",
        "repo_url": "git@github.com:example/sidecar.git",
        "base_branch": "main",
        "ports": [[5432, 5434]],
    }
    payload = req.model_dump(mode="python")
    payload["companions"] = [companion_req]
    req_with_companion = WorkspaceCreateRequest.model_validate(payload)

    async with factory() as session:
        source = await create_workspace_row(
            session,
            req_with_companion,
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    await _mark_failed(factory, source.id, release_runtime=False)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="auto-retry port conflict",
            provider_environ={},
            ignore_source_runtime_check=True,
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_runtime_gate_override_succeeds_when_source_runtime_released(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When ignore_source_runtime_check=True and the source runtime has been
    released, the retry can proceed because the source's host ports are no
    longer held and there is no conflict."""
    settings = _settings_with_host_home(tmp_path)
    req = _request_with_preflight_override()
    companion_req = {
        "name": "sidecar",
        "repo_url": "git@github.com:example/sidecar.git",
        "base_branch": "main",
        "ports": [[5432, 5434]],
    }
    payload = req.model_dump(mode="python")
    payload["companions"] = [companion_req]
    req_with_companion = WorkspaceCreateRequest.model_validate(payload)

    async with factory() as session:
        source = await create_workspace_row(
            session,
            req_with_companion,
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    await _mark_failed(factory, source.id, release_runtime=True)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="auto-retry source released",
            provider_environ={},
            ignore_source_runtime_check=True,
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_runtime_gate_override_succeeds_no_host_ports_runtime_not_released(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When ignore_source_runtime_check=True and there are no host-port
    claims, retry must succeed even when the source compose stack has not
    been released because there are no ports to collide on."""
    settings = _settings_with_host_home(tmp_path)
    req = _request_with_preflight_override()

    async with factory() as session:
        source = await create_workspace_row(
            session,
            req,
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    await _mark_failed(factory, source.id, release_runtime=False)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="auto-retry no ports",
            provider_environ={},
            ignore_source_runtime_check=True,
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_defaults_unset_worker_node_to_local_for_legacy_source_hostname(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """An upgraded local install may have failed source rows stamped with the
    old container hostname while current local workers default to "local".
    When the source runtime is already released, the retry reservation must be
    placed on "local" so the local scheduler can list and claim it."""
    settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
    )
    req = _request_with_preflight_override()
    companion_req = {
        "name": "sidecar",
        "repo_url": "git@github.com:example/sidecar.git",
        "base_branch": "main",
        "ports": [[5432, 5434]],
    }
    payload = req.model_dump(mode="python")
    payload["companions"] = [companion_req]
    req_with_companion = WorkspaceCreateRequest.model_validate(payload)

    async with factory() as session:
        source = await create_workspace_row(
            session,
            req_with_companion,
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    await _mark_failed(factory, source.id, release_runtime=True)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        ws.node_id = "legacy-container-hostname"
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            source.id,
        )
        assert len(reservations) == 1
        assert reservations[0].node_id == "local"
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="legacy local hostname normalization test",
            provider_environ={},
        )
        await session.commit()

    async with factory() as session:
        retried_reservations = await ResourceReservationRepository(
            session,
        ).list_for_workspace(retry.new_workspace.id)
        assert len(retried_reservations) == 1
        assert retried_reservations[0].node_id == "local"
