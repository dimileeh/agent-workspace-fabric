"""Service-level retry/requeue tests for terminal workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.db.repositories as repositories
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Operation, Task, TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import (
    QueueDecisionRepository,
    ResourceReservationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    build_planning_prompt,
    render_workspace_path,
)
from awf.service.conformance_salvage import (
    SALVAGE_BASE_UNAVAILABLE,
    ConformanceSalvageError,
)
from awf.service.workspaces import (
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryNotFoundError,
    WorkspaceRetrySalvageUnavailableError,
    WorkspaceService,
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


def _opencode_request() -> WorkspaceCreateRequest:
    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    return WorkspaceCreateRequest.model_validate(payload)


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
    text = '{"models":[{"name":"kimi-k2.6:cloud"}]}' if url.endswith("/api/tags") else "{}"
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
        (worktree / "tests/unit/test_retry.py").write_text("def test_retry():\n    assert True\n")
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
async def test_create_blocks_provider_readiness_before_rows(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await create_workspace_row(
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
async def test_create_runs_provider_preflight_probe_off_event_loop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        workspace = await create_workspace_row(
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
async def test_create_preflight_probes_profile_ollama_daemon(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A profile-declared Ollama base URL must drive the create-time readiness
    probe so admission targets the same daemon the executor's pre-agent step
    reaches — not the worker env's daemon (regression for PRRT_kwDOSJAM6s6JU0zF)."""
    settings = _settings_with_host_home(tmp_path)

    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    payload["workspace"] = {
        "profile_ref": "inline",
        "profile": {
            "name": "ollama-sidecar",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434",
                },
            },
        },
    }
    request = WorkspaceCreateRequest.model_validate(payload)

    probed: list[str] = []

    def _capturing_http_get(url: str, *, timeout: float) -> SimpleNamespace:
        probed.append(url)
        return _ollama_ok(url, timeout=timeout)

    async with factory() as session:
        workspace = await create_workspace_row(
            session,
            request,
            settings=settings,
            provider_environ={
                "OLLAMA_API_KEY": "ollama_secret",
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434",
            },
            run_subprocess=_docker_ok,
            http_get=_capturing_http_get,
        )

    preflight = workspace.task_policy["provider_readiness_preflight"]
    assert preflight["provider"] == "opencode"
    assert probed, "expected the create-time readiness probe to hit the Ollama daemon"
    assert all("ollama-sidecar:11434" in url for url in probed)
    assert all("worker-daemon" not in url for url in probed)


@pytest.mark.unit
async def test_create_with_provider_readiness_override_records_policy_and_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        workspace = await create_workspace_row(
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
async def test_create_successful_provider_preflight_emits_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    settings = _settings_with_host_home(tmp_path)

    async with factory() as session:
        workspace = await create_workspace_row(
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
        first = await create_workspace_row(
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
        first = await create_workspace_row(
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
async def test_retry_preflight_probes_source_profile_ollama_daemon(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A retry must overlay the source profile's Ollama base URL onto the
    readiness probe so admission targets the same daemon the executor's
    pre-agent step reaches — not the worker env's daemon (regression for
    PRRT_kwDOSJAM6s6JU4FX)."""
    settings = _settings_with_host_home(tmp_path)

    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    payload["workspace"] = {
        "profile_ref": "inline",
        "profile": {
            "name": "ollama-sidecar",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434",
                },
            },
        },
    }
    request = WorkspaceCreateRequest.model_validate(payload)

    async with factory() as session:
        first = await create_workspace_row(
            session,
            request,
            settings=settings,
            provider_environ={
                "OLLAMA_API_KEY": "ollama_secret",
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434",
            },
            run_subprocess=_docker_ok,
            http_get=_ollama_ok,
        )
        await session.commit()
    await _mark_failed(factory, first.id)

    probed: list[str] = []

    def _capturing_http_get(url: str, *, timeout: float) -> SimpleNamespace:
        probed.append(url)
        return _ollama_ok(url, timeout=timeout)

    async with factory() as session:
        await retry_workspace_row(
            session,
            first.id,
            settings=settings,
            provider_environ={
                "OLLAMA_API_KEY": "ollama_secret",
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434",
            },
            run_subprocess=_docker_ok,
            http_get=_capturing_http_get,
        )

    assert probed, "expected the retry readiness probe to hit the Ollama daemon"
    assert all("ollama-sidecar:11434" in url for url in probed)
    assert all("worker-daemon" not in url for url in probed)


@pytest.mark.unit
async def test_retry_with_provider_readiness_override_records_source_and_target(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
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


@pytest.mark.unit
async def test_retry_overlap_lookup_uses_source_workspace_id_for_requested_filtering(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    workspace_ids = iter(
        [
            "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
            "ws_bbbbbbbbbbbbbbbbbbbbbbbb",
            "ws_cccccccccccccccccccccccc",
        ]
    )
    monkeypatch.setattr(repositories, "new_workspace_id", lambda: next(workspace_ids))
    requested_path = "docs/ws_0123456789abcdef01234567.md"
    planning = {"plan_path": "docs/{workspace_id}.md"}
    payload_data = _request_with_preflight_override().model_dump(mode="python")
    payload_data["task"]["owned_paths"] = [requested_path]
    payload_data["workspace"] = {
        "profile_ref": None,
        "profile": {"name": "custom-planning", "planning": planning},
    }
    payload = WorkspaceCreateRequest.model_validate(payload_data)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        existing = await repo.create(
            repo_url=payload.repo.url,
            branch_base=payload.repo.base_branch,
            task_title="Active docs owner",
            task_prompt="Own a real ws-shaped docs file.",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            owned_paths=[requested_path],
            resolved_profile={"planning": planning},
        )
        source = await create_workspace_row(
            session,
            payload,
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    await _mark_failed(factory, source.id)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            source.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry service test fixture",
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    async with factory() as session:
        events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace.id,
                        WorkspaceEvent.event_type == "workspace.owned_path_overlap_risk",
                    )
                )
            ).scalars()
        )

    assert len(events) == 1
    assert events[0].payload["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
    assert events[0].payload["workspace_ids"] == [existing.id]
    assert events[0].payload["overlaps"] == [
        {
            "workspace_id": existing.id,
            "existing_path": requested_path,
            "requested_path": requested_path,
        }
    ]


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


async def _mark_conformance_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
) -> None:
    """Mark a workspace as failed with conformance-unsatisfied evidence."""
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
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def _mark_conformance_failed_without_evidence(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
) -> None:
    """Mark a conformance failure workspace without conformance evidence payload."""
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
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def _mark_agent_timeout_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
    reason_code: str = AGENT_IDLE_TIMEOUT,
) -> None:
    """Mark a workspace failure caused by agent timeout in agent phase."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent emitted no output for 3600.0 seconds"
        workspace.branch_name = "awf/ws_timeout"
        workspace.remote_push_branch = "awf/ws_timeout"
        workspace.base_commit = base_commit
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=reason_code,
            payload={
                "reason_code": reason_code,
                "message": "agent emitted no output for 3600.0 seconds",
                "details": {
                    "provider": "claude_code",
                    "model": "claude-opus-4-7",
                    "retryable": True,
                },
            },
        )
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
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
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def test_retry_failed_workspace_clones_v2_metadata_and_increments_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request())
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
                await session.execute(select(Operation).where(Operation.workspace_id == retried.id))
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
        "awf.service.workspaces_retry.get_settings",
        lambda: Settings(
            workspace_steady_cpu=3.0,
            workspace_steady_memory_gb=10.0,
            workspace_peak_cpu=6.0,
            workspace_peak_memory_gb=16.0,
        ),
    )
    service = WorkspaceService(factory)
    first = await service.create(_request())
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
            await ResourceReservationRepository(session).list_for_workspace(retry.new_workspace_id)
        )[0]

    assert retried_reservation.steady_cpu == 3.0
    assert retried_reservation.steady_memory_gb == 10.0
    assert retried_reservation.peak_cpu == 6.0
    assert retried_reservation.peak_memory_gb == 16.0


async def test_retry_legacy_dind_source_without_reservation_preserves_dind_demand(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    payload = _request().model_dump(mode="python")
    payload["workspace"] = {
        "profile_ref": None,
        "profile": {"name": "dind-retry", "docker": {"mode": "dind"}},
    }
    first = await service.create(WorkspaceCreateRequest.model_validate(payload))
    await _mark_failed(factory, first.id)

    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        assert source.resolved_profile["docker"]["mode"] == "dind"
        source_reservations = await ResourceReservationRepository(session).list_for_workspace(
            first.id
        )
        assert len(source_reservations) == 1
        await session.delete(source_reservations[0])
        await session.commit()

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        source_reservations = await ResourceReservationRepository(session).list_for_workspace(
            first.id
        )
        retried_reservation = (
            await ResourceReservationRepository(session).list_for_workspace(retry.new_workspace_id)
        )[0]

    assert source_reservations == []
    assert retried_reservation.dind_slots == 1


async def test_retry_legacy_inline_dind_source_without_resolved_profile_preserves_dind_demand(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    payload = _request().model_dump(mode="python")
    payload["workspace"] = {
        "profile_ref": None,
        "profile": {"name": "legacy-inline-dind-retry", "docker": {"mode": "dind"}},
    }
    first = await service.create(WorkspaceCreateRequest.model_validate(payload))
    await _mark_failed(factory, first.id)

    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        assert source.requested_profile is not None
        assert source.requested_profile["docker"]["mode"] == "dind"
        source.resolved_profile = None
        source_reservations = await ResourceReservationRepository(session).list_for_workspace(
            first.id
        )
        assert len(source_reservations) == 1
        await session.delete(source_reservations[0])
        await session.commit()

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        source_reservations = await ResourceReservationRepository(session).list_for_workspace(
            first.id
        )
        retried_reservation = (
            await ResourceReservationRepository(session).list_for_workspace(retry.new_workspace_id)
        )[0]

    assert source_reservations == []
    assert retried_reservation.dind_slots == 1


async def test_retry_source_without_reservation_or_profiles_defaults_dind_mode_to_none(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request())
    await _mark_failed(factory, first.id)

    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.requested_profile = None
        source.resolved_profile = None
        source_reservations = await ResourceReservationRepository(session).list_for_workspace(
            first.id
        )
        assert len(source_reservations) == 1
        await session.delete(source_reservations[0])
        await session.commit()

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        source_reservations = await ResourceReservationRepository(session).list_for_workspace(
            first.id
        )
        retried_reservation = (
            await ResourceReservationRepository(session).list_for_workspace(retry.new_workspace_id)
        )[0]
        retry_decisions = await QueueDecisionRepository(session).list_for_workspace(
            retry.new_workspace_id
        )

    assert source_reservations == []
    assert retried_reservation.dind_slots == 0
    assert retry_decisions[0].resource_summary["dind_slots"] == 0
    assert retry_decisions[0].resource_summary["dind_mode"] == "none"


@pytest.mark.unit
async def test_retry_conformance_unsatisfied_auto_salvages_implementation_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create(_request())
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
                await session.execute(select(Operation).where(Operation.workspace_id == retried.id))
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
    first = await service.create(_request())
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
@pytest.mark.parametrize("timeout_reason", [AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT])
async def test_retry_agent_idle_timeout_auto_salvages_implementation_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    timeout_reason: str,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create(_request())
    base_commit = _create_conformance_source_worktree(settings, first.id)
    await _mark_agent_timeout_failed(
        factory,
        first.id,
        base_commit=base_commit,
        reason_code=timeout_reason,
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
        operations = list(
            (
                await session.execute(select(Operation).where(Operation.workspace_id == retried.id))
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retried.id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    salvage = retried.task_policy["conformance_salvage"]
    assert salvage["salvage_kind"] == "agent_timeout"
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
    assert salvage["remaining_gaps"] == [
        "The previous agent run timed out before it could finish.",
        "Continue from the recovered implementation diff and complete the original task.",
    ]
    assert salvage["conformance_evidence_ref"] == {
        "source_workspace_id": first.id,
        "event_type": "workspace.state_changed",
        "reason_code": timeout_reason,
    }
    assert "Automatic AWF timeout salvage" in retried.task_prompt
    assert "Fix the intermittent validation failure." in retried.task_prompt
    assert "Continue from the recovered implementation" in retried.task_prompt
    assert operations[0].payload["source_reason_code"] == timeout_reason
    assert operations[0].payload["recovery_strategy"] == "continue_from_timeout_salvage"
    assert operations[0].payload["conformance_salvage"] == salvage
    assert operations[0].result["source_reason_code"] == timeout_reason
    assert operations[0].result["conformance_salvage"] == salvage
    assert retry_created[0].payload["source_reason_code"] == timeout_reason
    assert retry_created[0].payload["conformance_salvage"] == salvage


@pytest.mark.unit
@pytest.mark.parametrize("timeout_reason", [AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT])
async def test_retry_agent_idle_timeout_plan_only_diff_retries_without_salvage(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    timeout_reason: str,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create(_request())
    base_commit = _create_conformance_source_worktree(
        settings,
        first.id,
        implementation_diff=False,
    )
    await _mark_agent_timeout_failed(
        factory,
        first.id,
        base_commit=base_commit,
        reason_code=timeout_reason,
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
        operations = list(
            (
                await session.execute(select(Operation).where(Operation.workspace_id == retried.id))
            ).scalars()
        )

    assert "conformance_salvage" not in retried.task_policy
    assert retried.task_prompt == "Fix the intermittent validation failure."
    assert "source_reason_code" not in operations[0].payload
    assert "conformance_salvage" not in operations[0].payload


@pytest.mark.unit
async def test_retry_agent_idle_timeout_salvage_unavailable_errors_abort_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create(_request())
    base_commit = _create_conformance_source_worktree(
        settings,
        first.id,
        implementation_diff=True,
    )
    await _mark_agent_timeout_failed(factory, first.id, base_commit=base_commit)

    def _raise(**_kwargs: object) -> None:
        raise ConformanceSalvageError(
            reason_code=SALVAGE_BASE_UNAVAILABLE,
            message="Timeout salvage capture intentionally failed",
            detail={"base_commit": "missing"},
        )

    monkeypatch.setattr("awf.service.workspaces_retry.capture_conformance_salvage", _raise)

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
    assert exc_info.value.detail["reason_code"] == SALVAGE_BASE_UNAVAILABLE
    assert exc_info.value.detail["source_reason_code"] == AGENT_IDLE_TIMEOUT
    assert len(workspaces) == 1
    assert len(operations) == 0


@pytest.mark.unit
async def test_retry_conformance_plan_only_diff_fails_without_retry_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings_with_work_dir(tmp_path)
    service = WorkspaceService(factory)
    first = await service.create(_request())
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
    first = await service.create(_request())
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
    assert "during this retry planning phase" in retried.task_prompt
    assert "phase-scoped" in retried.task_prompt
    assert "After writing the plan, stop" not in retried.task_prompt
    assert "Prior source required plan paths from the failed planning attempt" in (
        retried.task_prompt
    )
    assert "Create or update only `docs/awf-plans/ws_scope_old.md`" not in (retried.task_prompt)
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


async def _seed_failed_source_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_kind: str,
) -> str:
    """Persist a source workspace for a task kind that bypasses direct create.

    ``sync_feature_pr`` is created through the PR-adoption flow (via
    ``WorkspaceRepository.create``), not the public request path, so retry
    coverage seeds the row the same way instead of building a rejected request.
    """
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/retryable.git",
            branch_base="development",
            task_title="Retry flaky validation",
            task_prompt="Fix the intermittent validation failure.",
            task_external_id="TICKET-RETRY",
            task_class="test_task",
            owned_paths=["src/awf/retry/**"],
            auto_merge=False,
            initial_review_grace_period_seconds=30,
            agent=AgentRuntime.codex.value,
            profile_ref="python",
            requested_profile={"source": "retry-test-profile"},
            resolved_profile={"source": "retry-test-profile"},
            test_commands=["uv run pytest tests/unit -q"],
            task_kind=task_kind,
        )
        await session.commit()
        return source.id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_kind", "branch_name", "remote_push_branch"),
    [
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
    if task_kind == "sync_release_pr":
        first_id = (await service.create(_request(task_kind=task_kind))).id
    else:
        first_id = await _seed_failed_source_workspace(factory, task_kind=task_kind)
    await _mark_planning_scope_failed(
        factory,
        first_id,
        branch_name=branch_name,
        remote_push_branch=remote_push_branch,
    )

    retry = await _retry_with_preflight_override(service, first_id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        original = await repo.get(first_id)
        retried = await repo.get(retry.new_workspace_id)

    assert original is not None
    assert retried is not None
    assert original.task_kind == task_kind
    assert original.branch_name == branch_name
    assert original.remote_push_branch == remote_push_branch

    assert retried.task_kind == task_kind
    assert retried.branch_name is None
    assert retried.remote_push_branch == remote_push_branch
