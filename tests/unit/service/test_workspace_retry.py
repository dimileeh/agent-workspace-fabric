"""Service-level retry/requeue tests for terminal workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.common.config import Settings
from awf.db.base import Base
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Operation, Task, TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import ResourceReservationRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    build_planning_prompt,
    render_workspace_path,
)
from awf.service.workspaces import (
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryNotFoundError,
    WorkspaceRetrySalvageUnavailableError,
    WorkspaceService,
    create_workspace_v2_row,
    retry_workspace_row,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _request(
    *,
    task_kind: str = "feature_branch_pr",
    provider_readiness_override: bool = True,
) -> WorkspaceCreateV2Request:
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
    return WorkspaceCreateV2Request.model_validate(payload)


def _request_with_preflight_override(
    *,
    reason: str = "operator verified provider readiness manually",
) -> WorkspaceCreateV2Request:
    payload = _request().model_dump(mode="json")
    payload["preflight"] = {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": reason,
    }
    return WorkspaceCreateV2Request.model_validate(payload)


def _opencode_request() -> WorkspaceCreateV2Request:
    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    return WorkspaceCreateV2Request.model_validate(payload)


def _settings_with_host_home(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
    )


def _ollama_provider_environ() -> dict[str, str]:
    return {
        "OLLAMA_API_KEY": "ollama_secret",
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }


def _docker_ok(args: list[str], **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="/usr/bin/cli\n", stderr="")

def _ollama_ok(url: str, *, timeout: float) -> SimpleNamespace:
    text = (
        '{"models":[{"name":"kimi-k2.6:cloud"}]}'
        if url.endswith("/api/tags")
        else "{}"
    )
    return SimpleNamespace(status_code=200, text=text)


def _ollama_ok_requiring_worker_thread(
    url: str,
    *,
    timeout: float,
) -> SimpleNamespace:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider preflight probe ran on the event-loop thread")
    return _ollama_ok(url, timeout=timeout)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _settings_with_work_dir(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        work_dir=str(tmp_path / "awf-state"),
        host_home=str(tmp_path / "home"),
        docker_host="",
    )


def _create_conformance_source_worktree(
    settings: Settings,
    workspace_id: str,
    *,
    implementation_diff: bool = True,
) -> str:
    worktree = Path(settings.work_dir) / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    _git(["init", "-q"], worktree)
    _git(["config", "user.name", "AWF Test"], worktree)
    _git(["config", "user.email", "awf@test.local"], worktree)
    (worktree / "src/awf").mkdir(parents=True)
    (worktree / "src/awf/retry.py").write_text("def retry():\n    return 'old'\n")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)

    (worktree / "docs/awf-plans").mkdir(parents=True)
    (worktree / "docs/awf-plans/ws_old.md").write_text("# Plan\n")
    (worktree / "docs/awf-plans/ws_old.conformance.json").write_text(
        '{"status":"needs_iteration"}\n'
    )
    if implementation_diff:
        (worktree / "src/awf/retry.py").write_text("def retry():\n    return 'new'\n")
        (worktree / "tests/unit").mkdir(parents=True)
        (worktree / "tests/unit/test_retry.py").write_text(
            "def test_retry():\n    assert True\n"
        )
    return base_commit


async def _retry_with_preflight_override(
    service: WorkspaceService,
    workspace_id: str,
) -> object:
    return await service.retry_workspace(
        workspace_id,
        provider_readiness_override=True,
        provider_readiness_override_reason="retry service test fixture",
    )


def test_retry_not_found_error_has_instance_detail() -> None:
    error = WorkspaceRetryNotFoundError("ws_missing")

    assert error.detail is None
    assert error.__dict__["detail"] is None


@pytest.mark.unit
async def test_create_v2_blocks_provider_readiness_before_rows(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await create_workspace_v2_row(
                session,
                _request(provider_readiness_override=False),
                settings=settings,
                provider_environ={},
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())
        attempts = list((await session.execute(select(TaskAttempt))).scalars())

    preflight = exc_info.value.detail["provider_readiness_preflight"]
    assert preflight["provider"] == "codex"
    assert preflight["model"] == "gpt-5.5"
    assert preflight["reason_code"] == "CODEX_AUTH_MISSING"
    assert preflight["blocks_launch"] is True
    assert workspaces == []
    assert attempts == []


@pytest.mark.unit
async def test_create_v2_runs_provider_preflight_probe_off_event_loop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        workspace = await create_workspace_v2_row(
            session,
            _opencode_request(),
            settings=settings,
            provider_environ=_ollama_provider_environ(),
            run_subprocess=_docker_ok,
            http_get=_ollama_ok_requiring_worker_thread,
        )

    preflight = workspace.task_policy["provider_readiness_preflight"]
    assert preflight["provider"] == "opencode"
    assert preflight["readiness_status"] == "ready"


@pytest.mark.unit
async def test_create_v2_with_provider_readiness_override_records_policy_and_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        workspace = await create_workspace_v2_row(
            session,
            _request_with_preflight_override(reason="manual local token refresh"),
            settings=settings,
            provider_environ={},
        )
        events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == workspace.id,
                        WorkspaceEvent.event_type == "workspace.provider_readiness_preflight",
                    )
                )
            ).scalars()
        )

    preflight = workspace.task_policy["provider_readiness_preflight"]
    assert preflight["readiness_status"] == "admitted_with_override"
    assert preflight["override_used"] is True
    assert preflight["override_reason"] == "manual local token refresh"
    assert preflight["reason_code"] == "CODEX_AUTH_MISSING"
    assert events[0].reason_code == "PROVIDER_READINESS_OVERRIDE_USED"
    assert events[0].payload["provider_readiness_preflight"] == preflight


@pytest.mark.unit
async def test_create_v2_successful_provider_preflight_emits_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        workspace = await create_workspace_v2_row(
            session,
            _request(provider_readiness_override=False),
            settings=settings,
            run_subprocess=_docker_ok,
        )
        events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == workspace.id,
                        WorkspaceEvent.event_type == "workspace.provider_readiness_preflight",
                    )
                )
            ).scalars()
        )

    preflight = workspace.task_policy["provider_readiness_preflight"]
    assert preflight["readiness_status"] == "ready"
    assert preflight["override_used"] is False
    assert events[0].reason_code == "PROVIDER_READINESS_READY"


@pytest.mark.unit
async def test_retry_blocks_provider_readiness_before_new_attempt(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_v2_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(factory, first.id)

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError):
            await retry_workspace_row(session, first.id, settings=settings, provider_environ={})

        workspaces = list((await session.execute(select(Workspace))).scalars())
        attempts = list((await session.execute(select(TaskAttempt))).scalars())

    assert [workspace.id for workspace in workspaces] == [first.id]
    assert [attempt.workspace_id for attempt in attempts] == [first.id]


@pytest.mark.unit
async def test_retry_runs_provider_preflight_probe_off_event_loop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    provider_environ = _ollama_provider_environ()
    async with factory() as session:
        first = await create_workspace_v2_row(
            session,
            _opencode_request(),
            settings=settings,
            provider_environ=provider_environ,
            run_subprocess=_docker_ok,
            http_get=_ollama_ok,
        )
        await session.commit()
    await _mark_failed(factory, first.id)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            settings=settings,
            provider_environ=provider_environ,
            run_subprocess=_docker_ok,
            http_get=_ollama_ok_requiring_worker_thread,
        )

    preflight = retry.new_workspace.task_policy["provider_readiness_preflight"]
    assert preflight["source_workspace_id"] == first.id
    assert preflight["readiness_status"] == "ready"


@pytest.mark.unit
async def test_retry_with_provider_readiness_override_records_source_and_target(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_v2_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(factory, first.id)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry after local auth repair",
            settings=settings,
            provider_environ={},
        )
        retried = await WorkspaceRepository(session).get(retry.new_workspace.id)

    assert retried is not None
    preflight = retried.task_policy["provider_readiness_preflight"]
    assert preflight["source_workspace_id"] == first.id
    assert preflight["provider"] == "codex"
    assert preflight["model"] == "gpt-5.5"
    assert preflight["override_used"] is True
    assert preflight["override_reason"] == "retry after local auth repair"


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    branch_name: str = "codex/old-attempt",
    remote_push_branch: str | None = None,
) -> dict[str, object]:
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
        await session.commit()
        return frozen_profile


async def _mark_conformance_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = (
            "plan conformance was not satisfied after 0 iteration(s): add tests"
        )
        workspace.branch_name = "awf/ws_old"
        workspace.remote_push_branch = "awf/ws_old"
        workspace.base_commit = base_commit
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                "details": {
                    "conformance": {
                        "summary": "Implementation is incomplete.",
                        "gaps": ["Add regression test", "Wire retry endpoint"],
                        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                        "iterations_used": 0,
                        "max_iterations": 0,
                        "plan_path": "docs/awf-plans/ws_old.md",
                        "report_path": "docs/awf-plans/ws_old.conformance.json",
                    }
                },
                "salvage": {
                    "hint": "Workspace worktree and branch were preserved for salvage.",
                    "worktree_path": "/worktrees/ws_old",
                    "branch_name": "awf/ws_old",
                    "remote_push_branch": "awf/ws_old",
                },
            },
        )
        await session.commit()


async def _mark_conformance_failed_without_evidence(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "plan conformance was not satisfied"
        workspace.branch_name = "awf/ws_old"
        workspace.remote_push_branch = "awf/ws_old"
        workspace.base_commit = base_commit
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                "details": {"conformance": "legacy-invalid"},
            },
        )
        await session.commit()


async def _mark_planning_scope_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    approved_fallback_model: str | None = None,
    branch_name: str = "awf/ws_scope_old",
    remote_push_branch: str | None = "awf/ws_scope_old",
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = (
            "planning phase changed files outside `docs/awf-plans/ws_scope_old.md`"
        )
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.task_policy = {
            **workspace.task_policy,
            **(
                {
                    "planning_scope_recovery": {
                        "approved_fallback_model": approved_fallback_model,
                    }
                }
                if approved_fallback_model is not None
                else {}
            ),
        }
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
            payload={
                "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                "message": workspace.failure_message,
                "details": {
                    "planning_scope": {
                        "scope_phase": "planning",
                        "required_paths": ["docs/awf-plans/ws_scope_old.md"],
                        "offending_paths": ["src/awf/runtime/planning.py"],
                        "offending_commands": [],
                        "recommended_action": (
                            "Retry planning from a clean workspace and salvage the "
                            "preserved branch only after explicit operator approval."
                        ),
                        "recovery_strategy": "discard_and_replan",
                        "salvage_policy": "explicit_salvage_required",
                    },
                    "recommended_action": (
                        "Retry planning from a clean workspace and salvage the preserved "
                        "branch only after explicit operator approval."
                    ),
                    "recovery_strategy": "discard_and_replan",
                    "salvage_policy": "explicit_salvage_required",
                },
                "salvage": {
                    "hint": "Workspace worktree and branch were preserved for salvage.",
                    "worktree_path": "/worktrees/ws_scope_old",
                    "branch_name": "awf/ws_scope_old",
                    "remote_push_branch": "awf/ws_scope_old",
                },
            },
        )
        await session.commit()

async def test_retry_failed_workspace_clones_v2_metadata_and_increments_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    frozen_profile = await _mark_failed(factory, first.id)

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        original = await WorkspaceRepository(session).get(first.id)
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retried.id)
                )
            ).scalars()
        )
        retry_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.event_type.in_(
                            ["workspace.retry_requested", "workspace.retry_created"]
                        )
                    )
                )
            ).scalars()
        )

    assert original is not None
    assert retried is not None
    assert retry.source_workspace_id == first.id
    assert retry.new_workspace_id != first.id
    assert retry.status == WorkspaceStatus.requested
    assert retry.attempt_number == 2

    assert retried.status == WorkspaceStatus.requested.value
    assert retried.repo_url == original.repo_url
    assert retried.branch_base == original.branch_base
    assert retried.task_title == original.task_title
    assert retried.task_prompt == original.task_prompt
    assert retried.task_external_id == original.task_external_id
    assert retried.task_class == original.task_class
    assert retried.owned_paths == original.owned_paths
    assert retried.auto_merge is False
    assert retried.initial_review_grace_period_seconds == 30
    assert retried.agent == AgentRuntime.codex.value
    assert retried.profile_ref == "python"
    assert retried.resolved_profile == frozen_profile
    assert retried.test_commands == ["uv run pytest tests/unit -q"]
    assert retried.failure_reason is None
    assert retried.failure_message is None
    assert retried.pr_url is None
    assert retried.compose_project_name is None

    assert len(tasks) == 1
    assert [attempt.workspace_id for attempt in attempts] == [first.id, retried.id]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}

    assert len(operations) == 1
    assert operations[0].workspace_id == retried.id
    assert operations[0].type == "retry"
    assert operations[0].status == "succeeded"
    assert operations[0].payload == {"source_workspace_id": first.id}
    assert operations[0].result == {
        "new_workspace_id": retried.id,
        "attempt_number": 2,
        "status": "requested",
    }

    assert {
        (event.workspace_id, event.event_type, event.payload["source_workspace_id"])
        for event in retry_events
        if event.payload is not None
    } == {
        (first.id, "workspace.retry_requested", first.id),
        (retried.id, "workspace.retry_created", first.id),
    }

async def test_retry_recomputes_resource_reservation_from_current_defaults(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "awf.service.workspaces.get_settings",
        lambda: Settings(
            workspace_steady_cpu=3.0,
            workspace_steady_memory_gb=10.0,
            workspace_peak_cpu=6.0,
            workspace_peak_memory_gb=16.0,
        ),
    )
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_failed(factory, first.id)

    async with factory() as session:
        source_reservation = (
            await ResourceReservationRepository(session).list_for_workspace(first.id)
        )[0]
        source_reservation.steady_cpu = 1.0
        source_reservation.steady_memory_gb = 4.0
        source_reservation.peak_cpu = 1.5
        source_reservation.peak_memory_gb = 7.0
        await session.commit()

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        retried_reservation = (
            await ResourceReservationRepository(session).list_for_workspace(
                retry.new_workspace_id
            )
        )[0]

    assert retried_reservation.steady_cpu == 3.0
    assert retried_reservation.steady_memory_gb == 10.0
    assert retried_reservation.peak_cpu == 6.0
    assert retried_reservation.peak_memory_gb == 16.0


@pytest.mark.unit
async def test_retry_conformance_unsatisfied_auto_salvages_implementation_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    base_commit = _create_conformance_source_worktree(settings, first.id)
    await _mark_conformance_failed(factory, first.id, base_commit=base_commit)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry service test fixture",
            settings=settings,
        )
        await session.commit()

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace.id)
        assert retried is not None
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retried.id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    salvage = retried.task_policy["conformance_salvage"]
    patch_path = Path(salvage["patch_path"])
    assert salvage["source_workspace_id"] == first.id
    assert salvage["source_base_commit"] == base_commit
    assert salvage["implementation_paths"] == [
        "src/awf/retry.py",
        "tests/unit/test_retry.py",
    ]
    assert salvage["plan_artifact_paths"] == [
        "docs/awf-plans/ws_old.conformance.json",
        "docs/awf-plans/ws_old.md",
    ]
    assert patch_path.exists()
    assert hashlib.sha256(patch_path.read_bytes()).hexdigest() == salvage["patch_sha256"]
    assert "docs/awf-plans" not in patch_path.read_text()
    assert "AWF automatically captured the prior implementation diff" in retried.task_prompt
    assert "Add regression test" in retried.task_prompt
    assert operations[0].payload["conformance_salvage"] == salvage
    assert operations[0].result["conformance_salvage"] == salvage
    assert any(
        event.event_type == "workspace.retry_created"
        and event.payload["conformance_salvage"] == salvage
        for event in retry_created
    )


@pytest.mark.unit
async def test_retry_conformance_unsatisfied_without_evidence_still_salvages_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    base_commit = _create_conformance_source_worktree(settings, first.id)
    await _mark_conformance_failed_without_evidence(
        factory,
        first.id,
        base_commit=base_commit,
    )

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry service test fixture",
            settings=settings,
        )
        await session.commit()

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace.id)

    assert retried is not None
    salvage = retried.task_policy["conformance_salvage"]
    assert salvage["source_workspace_id"] == first.id
    assert salvage["remaining_gaps"] == []
    assert salvage["conformance_evidence_ref"] is None


@pytest.mark.unit
async def test_retry_conformance_plan_only_diff_fails_without_retry_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    base_commit = _create_conformance_source_worktree(
        settings,
        first.id,
        implementation_diff=False,
    )
    await _mark_conformance_failed(factory, first.id, base_commit=base_commit)

    async with factory() as session:
        with pytest.raises(WorkspaceRetrySalvageUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry service test fixture",
                settings=settings,
            )

    async with factory() as session:
        workspaces = list((await session.execute(select(Workspace))).scalars())
        operations = list((await session.execute(select(Operation))).scalars())

    assert exc_info.value.error_code == "WORKSPACE_RETRY_SALVAGE_UNAVAILABLE"
    assert exc_info.value.detail["reason_code"] == "SALVAGE_NO_IMPLEMENTATION_DIFF"
    assert exc_info.value.detail["source_workspace_id"] == first.id
    assert exc_info.value.detail["plan_artifact_paths"] == [
        "docs/awf-plans/ws_old.conformance.json",
        "docs/awf-plans/ws_old.md",
    ]
    assert len(workspaces) == 1
    assert operations == []


@pytest.mark.unit
async def test_retry_planning_scope_violation_discards_premature_work_and_replans(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_planning_scope_failed(factory, first.id)

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        original = await WorkspaceRepository(session).get(first.id)
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert original is not None
    assert retried is not None
    assert original.branch_name == "awf/ws_scope_old"
    assert original.remote_push_branch == "awf/ws_scope_old"
    assert retried.branch_name is None
    assert retried.remote_push_branch is None
    assert retried.pr_url is None
    assert "Fix the intermittent validation failure." in retried.task_prompt
    assert "Discard the premature implementation from the failed planning attempt" in (
        retried.task_prompt
    )
    assert "Rerun planning against the configured plan artifact" in retried.task_prompt
    assert "Prior source required plan paths from the failed planning attempt" in (
        retried.task_prompt
    )
    assert "Create or update only `docs/awf-plans/ws_scope_old.md`" not in (
        retried.task_prompt
    )
    assert "src/awf/runtime/planning.py" in retried.task_prompt
    assert retried.task_policy.get("agent_model") is None
    assert isinstance(retried.resolved_profile, dict)
    planning_profile = retried.resolved_profile.get("planning")
    assert isinstance(planning_profile, dict)
    plan_template = planning_profile.get("plan_path")
    assert isinstance(plan_template, str)
    retry_plan_path = render_workspace_path(plan_template, workspace_id=retried.id)
    composed_planning_prompt = build_planning_prompt(
        task_prompt=retried.task_prompt,
        plan_path=retry_plan_path,
    )
    assert (
        "Create or update only the configured plan artifact "
        f"`{retry_plan_path.as_posix()}`" in composed_planning_prompt
    )
    assert composed_planning_prompt.count("Create or update only") == 1
    assert "Create or update only `docs/awf-plans/ws_scope_old.md`" not in (
        composed_planning_prompt
    )

    assert len(operations) == 1
    operation_payload = operations[0].payload
    assert operation_payload is not None
    assert operation_payload["source_reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert operation_payload["planning_scope_evidence_ref"] == {
        "source_workspace_id": first.id,
        "event_type": "workspace.state_changed",
        "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    }
    assert operation_payload["recovery_strategy"] == "discard_and_replan"
    assert operation_payload["salvage_policy"] == "explicit_salvage_required"
    assert operation_payload["salvage"]["branch_name"] == "awf/ws_scope_old"
    assert "fallback_model" not in operation_payload
    assert operations[0].result["source_reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert operations[0].result["recovery_strategy"] == "discard_and_replan"
    assert retry_created[0].payload["source_reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert retry_created[0].payload["salvage_policy"] == "explicit_salvage_required"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_kind", "branch_name", "remote_push_branch"),
    [
        ("monitor_release_pr", "release-monitor/ws_scope_old", "release/2026-05"),
        ("sync_release_pr", "release-sync/ws_scope_old", "development"),
        ("sync_feature_pr", "feature-sync/ws_scope_old", "contributors/fix-123"),
    ],
)
async def test_retry_planning_scope_violation_preserves_monitor_and_sync_remote_push_branch(
    factory: async_sessionmaker[AsyncSession],
    task_kind: str,
    branch_name: str,
    remote_push_branch: str,
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind=task_kind))
    await _mark_planning_scope_failed(
        factory,
        first.id,
        branch_name=branch_name,
        remote_push_branch=remote_push_branch,
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        original = await repo.get(first.id)
        retried = await repo.get(retry.new_workspace_id)

    assert original is not None
    assert retried is not None
    assert original.task_kind == task_kind
    assert original.branch_name == branch_name
    assert original.remote_push_branch == remote_push_branch

    assert retried.task_kind == task_kind
    assert retried.branch_name is None
    assert retried.remote_push_branch == remote_push_branch


@pytest.mark.unit
async def test_retry_planning_scope_violation_applies_only_approved_fallback_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_planning_scope_failed(
        factory,
        first.id,
        approved_fallback_model="gpt-5.5",
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert retried is not None
    assert retried.task_policy["agent_model"] == "gpt-5.5"
    assert operations[0].payload["fallback_model"] == {
        "model": "gpt-5.5",
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
    assert operations[0].result["fallback_model"]["model"] == "gpt-5.5"
    assert retry_created[0].payload["fallback_model"]["model"] == "gpt-5.5"


@pytest.mark.unit
async def test_retry_legacy_workspace_without_attempt_reuses_fallback_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/retryable.git",
            branch_base="development",
            task_title="Retry legacy validation",
            task_prompt="Fix a legacy workspace without task attempts.",
            task_external_id=None,
            task_class="test_task",
            owned_paths=[],
            auto_merge=False,
            initial_review_grace_period_seconds=30,
            agent=AgentRuntime.codex.value,
            profile_ref="python",
            requested_profile={"source": "legacy-test-profile"},
            resolved_profile={"source": "legacy-test-profile"},
            test_commands=["uv run pytest tests/unit -q"],
        )
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
        source_id = source.id

    service = WorkspaceService(factory)
    first_retry = await _retry_with_preflight_override(service, source_id)
    second_retry = await _retry_with_preflight_override(service, source_id)

    async with factory() as session:
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )

    assert len(tasks) == 1
    assert tasks[0].idempotency_key == f"retry-source-workspace:{source_id}"
    assert [attempt.workspace_id for attempt in attempts] == [
        first_retry.new_workspace_id,
        second_retry.new_workspace_id,
    ]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}


@pytest.mark.unit
async def test_retry_preserves_remote_push_branch_for_sync_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind="sync_release_pr"))
    await _mark_failed(
        factory,
        first.id,
        branch_name="release-sync/ws_old",
        remote_push_branch="development",
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        original = await repo.get(first.id)
        retried = await repo.get(retry.new_workspace_id)

    assert original is not None
    assert retried is not None
    assert original.task_kind == "sync_release_pr"
    assert original.branch_name == "release-sync/ws_old"
    assert original.remote_push_branch == "development"

    assert retried.task_kind == "sync_release_pr"
    assert retried.branch_name is None
    assert retried.remote_push_branch == "development"


@pytest.mark.unit
async def test_retry_persists_task_kind_without_post_insert_update() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind="sync_release_pr"))
    await _mark_failed(factory, first.id)

    statements: list[str] = []

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

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await _retry_with_preflight_override(service, first.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
        await engine.dispose()

    task_kind_updates = [
        statement
        for statement in statements
        if statement.startswith("update workspaces") and "task_kind" in statement
    ]
    assert task_kind_updates == []
