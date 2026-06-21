"""Service-level retry/requeue provider-readiness preflight tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.db.repositories as repositories
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY
from awf.service.workspaces import (
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryNotFoundError,
    WorkspaceRetryRecoveringInFlightError,
    create_workspace_row,
    retry_workspace_row,
)
from tests.unit.service._workspace_retry_helpers import (
    _docker_ok,
    _mark_failed,
    _ollama_ok,
    _ollama_ok_requiring_worker_thread,
    _ollama_provider_environ,
    _opencode_request,
    _request,
    _request_with_preflight_override,
    _settings_with_host_home,
    factory,
)

pytestmark = pytest.mark.unit

__all__ = ["factory"]


def test_retry_not_found_error_has_instance_detail() -> None:
    error = WorkspaceRetryNotFoundError("ws_missing")

    assert error.detail is None
    assert error.__dict__["detail"] is None


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


async def test_create_preflight_probes_profile_ollama_daemon(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A profile-declared Ollama base URL must drive the create-time readiness
    probe so admission targets the same daemon the executor's pre-agent step
    reaches — not the worker env's daemon (regression for PRRT_kwDOSJAM6s6JU0zF).

    Both URLs are worker-reachable host-gateway aliases so the create-time daemon
    probe actually runs for the ``:cloud`` model: a non-worker-reachable sidecar URL
    now defers the probe to the agent container (PRRT_kwDOSJAM6s6JV_Rl), which would
    leave nothing to assert the overlay against. The two distinct gateway aliases are
    used rather than ``localhost`` / ``127.0.0.1`` because host-local targets now both
    normalize to ``host.docker.internal`` (issue #579), which would make the
    profile-vs-worker daemon indistinguishable here; ``gateway.docker.internal`` is
    worker-reachable and not collapsed by that normalization."""
    settings = _settings_with_host_home(tmp_path)

    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    payload["workspace"] = {
        "profile_ref": "inline",
        "profile": {
            "name": "ollama-profile-host",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "http://gateway.docker.internal:11434",
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
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://host.docker.internal:11434",
            },
            run_subprocess=_docker_ok,
            http_get=_capturing_http_get,
        )

    preflight = workspace.task_policy["provider_readiness_preflight"]
    assert preflight["provider"] == "opencode"
    assert probed, "expected the create-time readiness probe to hit the Ollama daemon"
    assert all("gateway.docker.internal:11434" in url for url in probed)
    assert all("host.docker.internal" not in url for url in probed)


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


async def test_retry_preflight_probes_source_profile_ollama_daemon(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A retry must overlay the source profile's Ollama base URL onto the
    readiness probe so admission targets the same daemon the executor's
    pre-agent step reaches — not the worker env's daemon (regression for
    PRRT_kwDOSJAM6s6JU4FX).

    Both URLs are worker-reachable host-gateway aliases so the retry daemon probe
    actually runs for the ``:cloud`` model: a non-worker-reachable sidecar URL now
    defers the probe to the agent container (PRRT_kwDOSJAM6s6JV_Rl), which would
    leave nothing to assert the overlay against. The two distinct gateway aliases are
    used rather than ``localhost`` / ``127.0.0.1`` because host-local targets now both
    normalize to ``host.docker.internal`` (issue #579), which would make the
    source-profile-vs-retry-env daemon indistinguishable here."""
    settings = _settings_with_host_home(tmp_path)

    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    payload["workspace"] = {
        "profile_ref": "inline",
        "profile": {
            "name": "ollama-profile-host",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "http://gateway.docker.internal:11434",
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
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://gateway.docker.internal:11434",
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
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://host.docker.internal:11434",
            },
            run_subprocess=_docker_ok,
            http_get=_capturing_http_get,
        )

    assert probed, "expected the retry readiness probe to hit the Ollama daemon"
    assert all("gateway.docker.internal:11434" in url for url in probed)
    assert all("host.docker.internal" not in url for url in probed)


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


async def _recovering_source(
    factory: async_sessionmaker[AsyncSession],
    *,
    not_before: str | None,
) -> str:
    """Persist a source workspace paused mid-run in the ``recovering`` status.

    Mirrors the slice-2 in-place provider retry state: the workspace held its
    warm stack + execution claim, recorded the cooldown ETA in ``task_policy``,
    and is awaiting the worker's post-cooldown resume.
    """
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/recovering.git",
            branch_base="development",
            task_title="Recovering flaky provider",
            task_prompt="Continue after the provider cooldown.",
            task_external_id="TICKET-RECOVERING",
            task_class="test_task",
            owned_paths=["src/awf/recovering/**"],
            auto_merge=False,
            initial_review_grace_period_seconds=30,
            agent=AgentRuntime.codex.value,
            profile_ref="python",
            requested_profile={"source": "recovering-test-profile"},
            resolved_profile={"source": "recovering-test-profile"},
            test_commands=["uv run pytest tests/unit -q"],
            task_kind="feature_branch_pr",
        )
        for status in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.recovering,
        ):
            await repo.transition(source, to=status, reason_code="TEST")
        recovery_state: dict[str, object] = {}
        if not_before is not None:
            recovery_state["not_before"] = not_before
        source.task_policy = {
            **source.task_policy,
            PROVIDER_RECOVERY_STATE_KEY: recovery_state,
        }
        await session.commit()
        return source.id


async def test_retry_on_recovering_workspace_is_noop_with_eta(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    not_before = "2026-06-21T12:30:00+00:00"
    source_id = await _recovering_source(factory, not_before=not_before)

    async with factory() as session:
        before = list((await session.execute(select(Workspace))).scalars())

        with pytest.raises(WorkspaceRetryRecoveringInFlightError) as exc_info:
            await retry_workspace_row(session, source_id)

        after = list((await session.execute(select(Workspace))).scalars())

    error = exc_info.value
    assert error.error_code == "WORKSPACE_AUTO_RETRY_IN_FLIGHT"
    assert not_before in error.message
    assert "cooldown" in error.message
    assert error.detail == {
        "status": "recovering",
        "provider_cooldown_not_before": not_before,
        "reason": "auto_retry_in_flight",
    }
    # No duplicate workspace was created by the colliding manual retry.
    assert [workspace.id for workspace in after] == [workspace.id for workspace in before]
    assert [workspace.id for workspace in after] == [source_id]


async def test_retry_recovering_error_handles_missing_cooldown(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _recovering_source(factory, not_before=None)

    async with factory() as session:
        with pytest.raises(WorkspaceRetryRecoveringInFlightError) as exc_info:
            await retry_workspace_row(session, source_id)

    error = exc_info.value
    assert error.error_code == "WORKSPACE_AUTO_RETRY_IN_FLIGHT"
    assert "resumes after the provider cooldown" in error.message
    assert error.detail == {
        "status": "recovering",
        "provider_cooldown_not_before": None,
        "reason": "auto_retry_in_flight",
    }
