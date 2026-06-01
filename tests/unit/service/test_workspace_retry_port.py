"""Port-conflict retry tests for terminal workspaces."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    QueueDecisionRepository,
    ResourceReservationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.workspaces import (
    WorkspaceCreateHostPortConflictError,
    WorkspaceRetrySourceRuntimeNotReleasedError,
    create_workspace_row,
    retry_workspace_row,
)
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
        workspace.pr_url = "https://github.com/example/retryable/pull/10"
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
async def test_retry_rejects_host_port_conflict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Retrying a workspace whose companion host port is still held by another
    active workspace must raise WorkspaceCreateHostPortConflictError instead
    of silently creating a workspace that will fail during compose-up."""
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

    await _mark_failed(factory, source.id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        blocker = await repo.create(
            repo_url="git@github.com:example/blocker.git",
            branch_base="main",
            task_title="Block port",
            task_prompt="noop",
            task_external_id=None,
            task_class="test_task",
            owned_paths=[],
            task_policy={
                "companions": [
                    {"name": "blocker-svc", "ports": [[5432, 5434]]},
                ],
            },
            auto_merge=False,
            initial_review_grace_period_seconds=0,
            agent="codex",
            env_profile=None,
            profile_ref=None,
            requested_profile=None,
            resolved_profile=None,
            test_commands=[],
            requires_database=False,
            idempotency_key=None,
            task_kind="feature_branch_pr",
            remote_push_branch=None,
        )
        blocker.node_id = "local"
        await repo.transition(blocker, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(blocker, to=WorkspaceStatus.ready, reason_code="TEST")
        await repo.transition(blocker, to=WorkspaceStatus.running, reason_code="TEST")
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceCreateHostPortConflictError):
            await retry_workspace_row(
                session,
                source.id,
                settings=settings,
                provider_readiness_override=True,
                provider_readiness_override_reason="host port conflict regression test",
                provider_environ={},
            )


@pytest.mark.unit
async def test_retry_rejects_host_port_conflict_with_normalized_target_node(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Retry admission must normalize the configured worker node before
    scanning host-port conflicts, matching create admission reservations."""
    settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
        worker_node_id=" node-a ",
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

    await _mark_failed(factory, source.id)

    async with factory() as session:
        blocker = await create_workspace_row(
            session,
            req_with_companion,
            settings=settings,
            provider_environ={},
        )
        blocker_reservations = await ResourceReservationRepository(
            session,
        ).list_for_workspace(blocker.id)
        assert len(blocker_reservations) == 1
        assert blocker_reservations[0].node_id == "node-a"
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceCreateHostPortConflictError):
            await retry_workspace_row(
                session,
                source.id,
                settings=settings,
                provider_readiness_override=True,
                provider_readiness_override_reason="normalized target node conflict test",
                provider_environ={},
            )


@pytest.mark.unit
async def test_retry_allows_same_port_when_source_is_only_holder(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Retrying must succeed when the only workspace holding the companion host
    port is the failed source itself and its terminal runtime has been
    released (compose stack torn down).  Only then is the host port free."""
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
        assert source.task_policy is not None
        assert "companions" in source.task_policy
        await session.commit()

    await _mark_failed(factory, source.id)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="host port same-holder test",
            provider_environ={},
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_rejects_host_port_conflict_with_source(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Retrying a failed workspace that still holds its companion host port
    (no terminal_runtime_released event) must raise
    WorkspaceRetrySourceRuntimeNotReleasedError.  The source's compose stack
    may still be running, so a separate fast-fail check detects this before
    the generic conflict query, yielding a clearer error message."""
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
        with pytest.raises(WorkspaceRetrySourceRuntimeNotReleasedError):
            await retry_workspace_row(
                session,
                source.id,
                settings=settings,
                provider_readiness_override=True,
                provider_readiness_override_reason="host port source conflict test",
                provider_environ={},
            )


@pytest.mark.unit
async def test_retry_allows_when_source_compose_project_name_is_none(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """A workspace that failed before compose-up (compose_project_name is None)
    never bound host ports, so retrying it must not raise
    WorkspaceRetrySourceRuntimeNotReleasedError even when no
    terminal_runtime_released event exists."""
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

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
        ws.failure_reason = "clone_failure"
        ws.failure_message = "git clone failed"
        ws.compose_project_name = None
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="compose_project_name is None retry",
            provider_environ={},
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_allows_when_no_host_ports_even_if_source_compose_stack_running(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """A source workspace with compose_project_name set but no host ports
    (no companions, no resolved-profile ports) must NOT raise
    WorkspaceRetrySourceRuntimeNotReleasedError — the compose project
    name is workspace-ID-scoped (awf_<id>), so a zero-port workspace
    cannot cause host-port conflicts with the retry.  The
    runtime-release guard is only applied inside the if host_ports:
    branch where it provides actual safety."""
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

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
        ws.failure_reason = "compose_up_failure"
        ws.failure_message = "compose up failed"
        ws.compose_project_name = "awf_stuck_stack"
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()

    async with factory() as session:
        result = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="no-host-ports runtime guard test",
            provider_environ={},
        )
        assert result.source_workspace_id == source.id


@pytest.mark.unit
async def test_retry_allows_when_target_node_differs_from_source(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When the retry targets a different node than the source, the
    runtime-release gate must be skipped — the source's unreleased
    compose stack on node A does not block retry placement on node B."""
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
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        ws.node_id = "node-a"
        await session.commit()

    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            source.id, limit=1
        )
        if reservations:
            reservations[0].node_id = "node-b"
            await session.commit()

    different_node_settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
        worker_node_id="node-b",
    )

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=different_node_settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="target node differs test",
            provider_environ={},
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_allows_when_source_node_id_is_none_but_reservation_on_different_node(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When source.node_id is None (compose-launch failure before the
    ready/success path fills it) but the source's reservation records a
    different node than the retry target, the runtime-release gate must
    use the reservation's node_id as the source's effective node and allow
    the retry.  The old code checked source.node_id directly, which was
    always None for this failure mode, causing a false
    SOURCE_RUNTIME_NOT_RELEASED error."""
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
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        ws.node_id = None
        await session.commit()

    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            source.id, limit=1
        )
        if reservations:
            reservations[0].node_id = "node-a"
            await session.commit()

    different_node_settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
        worker_node_id="node-b",
    )

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=different_node_settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="reservation node differs test",
            provider_environ={},
        )
        assert retry.new_workspace.id != source.id


@pytest.mark.unit
async def test_retry_persist_reservation_when_source_has_none(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Retrying a legacy source with no ResourceReservation but a known
    target_node_id (from settings.worker_node_id) must still persist a
    reservation for the retried workspace so that subsequent
    find_host_port_conflicts can see the retried workspace's node
    placement.  Without this, a later create/retry on the same node
    misses the retried workspace's host port claims."""
    settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
        worker_node_id="node-1",
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
        for res in await ResourceReservationRepository(session).list_for_workspace(
            source.id,
        ):
            await session.delete(res)
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="legacy no-reservation test",
            provider_environ={},
        )
        await session.commit()

    async with factory() as session:
        retried_reservations = await ResourceReservationRepository(
            session,
        ).list_for_workspace(retry.new_workspace.id)
        retry_decisions = await QueueDecisionRepository(session).list_for_workspace(
            retry.new_workspace.id,
        )
        assert len(retried_reservations) == 1
        assert retried_reservations[0].node_id == "node-1"
        assert len(retry_decisions) == 1
        assert retry_decisions[0].resource_summary == {}


@pytest.mark.unit
async def test_retry_no_reservation_when_target_node_unknown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When the source has no reservation and no worker_node_id is configured,
    target_node_id falls back to "local" (matching create-admission behaviour)
    and a reservation is created on the local node so the COALESCE-based
    conflict checker can detect port collisions on that node."""
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
        ws = await WorkspaceRepository(session).get(source.id)
        assert ws is not None
        ws.node_id = None
        for res in await ResourceReservationRepository(session).list_for_workspace(
            source.id,
        ):
            await session.delete(res)
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="no node test",
            provider_environ={},
        )
        await session.commit()

    async with factory() as session:
        retried_reservations = await ResourceReservationRepository(
            session,
        ).list_for_workspace(retry.new_workspace.id)
        assert len(retried_reservations) == 1
        assert retried_reservations[0].node_id == "local"


@pytest.mark.unit
async def test_retry_rejects_host_port_conflict_when_target_node_unknown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When target_node_id falls back to "local", the conflict scan on that
    node must detect an active workspace holding the same host port so the
    retry is rejected with a 409-class error."""
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
        ws = await WorkspaceRepository(session).get(source.id)
        assert ws is not None
        ws.node_id = None
        for res in await ResourceReservationRepository(session).list_for_workspace(
            source.id,
        ):
            await session.delete(res)
        await session.commit()

    async with factory() as session:
        repo = WorkspaceRepository(session)
        blocker = await repo.create(
            repo_url="git@github.com:example/blocker.git",
            branch_base="main",
            task_title="Block port",
            task_prompt="noop",
            task_external_id=None,
            task_class="test_task",
            owned_paths=[],
            task_policy={
                "companions": [
                    {"name": "blocker-svc", "ports": [[5432, 5434]]},
                ],
            },
            auto_merge=False,
            initial_review_grace_period_seconds=0,
            agent="codex",
            env_profile=None,
            profile_ref=None,
            requested_profile=None,
            resolved_profile=None,
            test_commands=[],
            requires_database=False,
            idempotency_key=None,
            task_kind="feature_branch_pr",
            remote_push_branch=None,
        )
        blocker.node_id = "local"
        await repo.transition(blocker, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(blocker, to=WorkspaceStatus.ready, reason_code="TEST")
        await repo.transition(blocker, to=WorkspaceStatus.running, reason_code="TEST")
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceCreateHostPortConflictError):
            await retry_workspace_row(
                session,
                source.id,
                settings=settings,
                provider_readiness_override=True,
                provider_readiness_override_reason="no-node conflict scan test",
                provider_environ={},
            )


@pytest.mark.unit
async def test_retry_auto_retry_excludes_source_from_port_conflict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When ignore_source_runtime_check=True (auto-retry path), the
    runtime-not-released gate is skipped, and the source workspace
    is excluded from port conflict scanning because the retry replaces
    the source.  Even though the source still holds host ports (no
    runtime release event), retry succeeds because excluding the
    source avoids a false conflict with itself."""
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
async def test_retry_auto_retry_succeeds_when_source_runtime_released(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When ignore_source_runtime_check=True AND the source runtime has been
    released, the auto-retry can proceed because the source's host ports
    are no longer held and there is no conflict."""
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
async def test_retry_auto_retry_succeeds_no_host_ports_runtime_not_released(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When ignore_source_runtime_check=True and there are no host-port
    claims, the auto-retry must succeed even when the source compose
    stack has not been released — there are no ports to collide on."""
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
async def test_retry_prefers_stamped_source_node_id_over_reservation_node_id(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When the source workspace has a stamped node_id (set by the provisioner
    to the actual hostname) that differs from the reservation's node_id
    (recorded as "local" at create time), the retry must prefer the stamped
    source.node_id for target_node_id.  Without this, target_node_id falls
    back to "local" while source_effective_node_id is the hostname, so the
    runtime-release guard sees them as different nodes and does not block;
    the conflict scan then queries the wrong node, missing the source's
    port claims, and a retry can proceed while the source stack still
    holds the port on the same physical host."""
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

    await _mark_failed(factory, source.id, release_runtime=False)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        ws.node_id = "worker-host-1"
        await session.commit()

    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            source.id, limit=1
        )
        if reservations:
            reservations[0].node_id = "local"
            await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetrySourceRuntimeNotReleasedError):
            await retry_workspace_row(
                session,
                source.id,
                settings=settings,
                provider_readiness_override=True,
                provider_readiness_override_reason="stamped node id vs reservation node id test",
                provider_environ={},
            )
