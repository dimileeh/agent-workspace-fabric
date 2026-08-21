"""Service-level retry/requeue provider-readiness preflight tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.db.repositories as repositories
import awf.service.workspaces_retry as workspaces_retry_service
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.node.provisioner_helpers import _provision_base_commit, _provision_checkout_base_branch
from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY
from awf.service.workspaces import (
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryPrAlreadyMergedError,
    WorkspaceRetryPrStateUnavailableError,
    WorkspaceRetryRecoveringInFlightError,
    create_workspace_row,
    retry_workspace_row,
)
from awf.service.workspaces_retry import (
    _live_pr_snapshot,
    _prune_and_migrate_retired_agent,
    _prune_retired_fallbacks,
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


def _live_pr_state(
    lifecycle: PullRequestLifecycle,
) -> Callable[[Workspace, int], Awaitable[PullRequestLifecycle]]:
    async def _check(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
        return lifecycle

    return _check


async def test_live_pr_snapshot_uses_current_forge_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeForgeClient:
        async def __aenter__(self) -> FakeForgeClient:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def fetch_pull_request_snapshot(self, **kwargs: object) -> PullRequestSnapshot:
            calls.append(kwargs)
            return PullRequestSnapshot(
                lifecycle=PullRequestLifecycle.open,
                head_ref="contributors/live-head",
                base_sha="b" * 40,
            )

    monkeypatch.setattr(
        workspaces_retry_service,
        "make_forge_client",
        lambda _forge, _runner: FakeForgeClient(),
    )
    source = SimpleNamespace(
        repo_url="git@github.com:example/retryable.git",
        resolved_profile={"forge": "github"},
    )

    snapshot = await _live_pr_snapshot(source, 10)

    assert snapshot == PullRequestSnapshot(
        lifecycle=PullRequestLifecycle.open,
        head_ref="contributors/live-head",
        base_sha="b" * 40,
    )
    assert calls == [
        {
            "repo": workspaces_retry_service.RepoRef(
                owner="example",
                name="retryable",
                forge="github",
            ),
            "pr_number": 10,
        }
    ]


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
    assert preflight["model"] == "gpt-5.6-sol"
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
    assert preflight["model"] == "gpt-5.6-sol"
    assert preflight["override_used"] is True
    assert preflight["override_reason"] == "retry after local auth repair"


@pytest.mark.parametrize(
    ("remote_push_branch", "expected_remote_push_branch"),
    [
        ("contributors/existing-head", "contributors/existing-head"),
        (None, "awf/original-feature"),
    ],
)
async def test_retry_preserves_existing_feature_pr_identity(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    remote_push_branch: str | None,
    expected_remote_push_branch: str,
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/original-feature",
        remote_push_branch=remote_push_branch,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry existing PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == expected_remote_push_branch
    assert retried.base_commit == "b" * 40
    assert _provision_checkout_base_branch(retried) == expected_remote_push_branch


@pytest.mark.parametrize(
    ("pr_url", "expected_pr_number"),
    [
        ("https://github.com/example/retryable/pull/10", 10),
        ("https://bitbucket.org/example/retryable/pull-requests/11", 11),
    ],
)
async def test_retry_recovers_missing_feature_pr_number_from_url(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    pr_url: str,
    expected_pr_number: int,
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/legacy-feature",
        remote_push_branch=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_url = pr_url
        source.pr_number = None
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="recover legacy PR identity",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.pr_url == pr_url
    assert retried.pr_number == expected_pr_number
    assert retried.remote_push_branch == "awf/legacy-feature"
    assert _provision_checkout_base_branch(retried) == "awf/legacy-feature"


async def test_retry_rejects_open_feature_pr_without_persisted_head_ref(
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/lost-feature-head",
        remote_push_branch=None,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.branch_name = None
        source.pr_number = 10
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "pr_url": "https://github.com/example/retryable/pull/10",
        "reason_code": "PR_HEAD_REF_UNAVAILABLE",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_recovers_open_feature_pr_head_ref_from_live_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/lost-feature-head",
        remote_push_branch=None,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.branch_name = None
        source.pr_number = 10
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/live-feature-head",
            base_sha="c" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry existing PR",
            settings=settings,
            provider_environ={},
        )

    retried = retry.new_workspace
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == "contributors/live-feature-head"
    assert retried.base_commit == "c" * 40
    assert _provision_checkout_base_branch(retried) == "contributors/live-feature-head"
    assert _provision_base_commit(retried, checked_out_head="h" * 40) == "c" * 40


async def test_retry_rejects_open_feature_pr_without_persisted_or_live_base_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/legacy-feature",
        remote_push_branch="contributors/live-feature-head",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = None
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/live-feature-head",
            base_sha=None,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "pr_url": "https://github.com/example/retryable/pull/10",
        "reason_code": "PR_BASE_COMMIT_UNAVAILABLE",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_prefers_live_open_pr_head_over_stale_persisted_refs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/stale-local-head",
        remote_push_branch="contributors/stale-remote-head",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/current-live-head",
            base_sha="c" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry renamed PR head",
            settings=settings,
            provider_environ={},
        )

    retried = retry.new_workspace
    assert retried.remote_push_branch == "contributors/current-live-head"
    assert retried.base_commit == "b" * 40
    assert _provision_checkout_base_branch(retried) == "contributors/current-live-head"


async def test_retry_replaces_feature_pr_closed_externally(
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/closed-feature",
        remote_push_branch="contributors/closed-head",
        failure_reason_code="pr_closed_externally",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="replace closed PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert retried.pr_url is None
    assert retried.pr_number is None
    assert retried.remote_push_branch is None
    assert _provision_checkout_base_branch(retried) == retried.branch_base


async def test_retry_preserves_feature_pr_reopened_after_external_close(
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/reopened-feature",
        remote_push_branch="contributors/reopened-head",
        failure_reason_code="pr_closed_externally",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="reuse reopened PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == "contributors/reopened-head"
    assert _provision_checkout_base_branch(retried) == "contributors/reopened-head"


async def test_retry_replaces_feature_pr_closed_after_unrelated_failure(
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/later-closed-feature",
        remote_push_branch="contributors/later-closed-head",
        failure_reason_code="VALIDATION_FAILED",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="replace PR closed after failure",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    assert retried.pr_url is None
    assert retried.pr_number is None
    assert retried.remote_push_branch is None
    assert _provision_checkout_base_branch(retried) == retried.branch_base


async def test_retry_rejects_feature_pr_merged_after_failure(
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/merged-feature",
        remote_push_branch="contributors/merged-head",
        failure_reason_code="VALIDATION_FAILED",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrAlreadyMergedError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="reject merged PR retry",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.merged),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "pr_url": "https://github.com/example/retryable/pull/10",
        "reason_code": "PR_ALREADY_MERGED",
    }
    assert exc_info.value.error_code == "PR_ALREADY_MERGED"
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_blocks_when_existing_feature_pr_state_is_unavailable(
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
    await _mark_failed(
        factory,
        first.id,
        pr_url="https://github.com/example/retryable/pull/10",
    )

    async def unavailable(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
        raise ForgeClientError("forge unavailable")

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=unavailable,
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "reason_code": "PR_STATE_LOOKUP_FAILED",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_keeps_caller_held_workspace_attached_for_post_retry_events(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Callers that already loaded the source must still be able to add_event after retry.

    Planning-scope auto-retry loads the workspace, calls retry_workspace_row in the
    same session, then appends a success marker on that same instance. Expunging the
    unlocked preview must not detach the caller's identity-mapped row.
    """
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        source = await create_workspace_row(
            session,
            _request_with_preflight_override(reason="caller-held workspace"),
            settings=settings,
            provider_environ={},
        )
        source_id = source.id
        await session.commit()
    await _mark_failed(factory, source_id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(source_id)
        assert workspace is not None
        retry = await retry_workspace_row(
            session,
            source_id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="caller-held workspace",
            provider_environ={},
        )
        await repo.add_event(
            workspace,
            event_type="workspace.planning_scope_auto_retry_requested",
            reason_code="PLANNING_SCOPE_AUTO_RETRY_REQUESTED",
            payload={
                "source_reason_code": "AGENT_PLAN_PHASE_SCOPE_VIOLATION",
                "new_workspace_id": retry.new_workspace.id,
            },
        )
        await session.commit()

    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == source_id)
                )
            )
            .scalars()
            .all()
        )
    assert any(
        event.event_type == "workspace.planning_scope_auto_retry_requested" for event in events
    )
    assert any(event.event_type == "workspace.retry_requested" for event in events)


async def test_retry_blocks_when_prefetched_feature_pr_state_mismatches_locked_row(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Fail closed when locked preserve identity does not match unlocked prefetch.

    Under the row lock we must not re-enter RetryPolicy.READ forge sleeps; a
    missing/mismatched prefetch therefore maps to PR_STATE_LOOKUP_FAILED.
    """
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        await session.commit()

    async def _skip_prefetch(_source: Workspace, *, pr_lifecycle_checker: object) -> None:
        return None

    monkeypatch.setattr(
        workspaces_retry_service,
        "_prefetch_existing_feature_pr_state",
        _skip_prefetch,
    )

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="prefetch mismatch",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "reason_code": "PR_STATE_LOOKUP_FAILED",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_propagates_programming_error_from_pr_state_lookup(
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
    await _mark_failed(
        factory,
        first.id,
        pr_url="https://github.com/example/retryable/pull/10",
    )

    async def broken(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
        raise TypeError("injected checker signature bug")

    async with factory() as session:
        with pytest.raises(TypeError, match="injected checker signature bug"):
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=broken,
            )


async def test_retry_ignores_stale_closed_pr_failure_after_remonitor(
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
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/remonitored-feature",
        remote_push_branch="contributors/open-head",
        failure_reason_code="pr_closed_externally",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        source.status = WorkspaceStatus.monitoring_pr.value
        await repo.add_event_with_states(
            source,
            event_type="workspace.remonitor_requested",
            old_state=WorkspaceStatus.failed,
            new_state=WorkspaceStatus.monitoring_pr,
            reason_code="OPERATOR_REMONITOR",
        )
        await repo.transition(
            source,
            to=WorkspaceStatus.failed,
            reason_code="VALIDATION_FAILED",
        )
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry current open PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == "contributors/open-head"
    assert _provision_checkout_base_branch(retried) == "contributors/open-head"


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


async def test_retry_prunes_retired_fallbacks_from_cloned_policy(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        first.task_policy = {
            **first.task_policy,
            "provider_recovery": {
                "fallbacks": [
                    {"agent": "gemini", "model": "gemini-1.5-pro"},
                    {"agent": "codex", "model": "gpt-5.5"},
                ]
            },
        }
        await session.commit()
    await _mark_failed(factory, first.id)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            settings=settings,
            run_subprocess=_docker_ok,
        )

    retried_policy = retry.new_workspace.task_policy
    fallbacks = retried_policy.get("provider_recovery", {}).get("fallbacks", [])
    assert len(fallbacks) == 2
    assert fallbacks[0] is None
    assert isinstance(fallbacks[1], dict) and fallbacks[1].get("agent") == "codex"
    assert not any(isinstance(item, dict) and item.get("agent") == "gemini" for item in fallbacks)
    assert any(isinstance(item, dict) and item.get("agent") == "codex" for item in fallbacks)
    preflight = retried_policy["provider_readiness_preflight"]
    assert preflight["readiness_status"] == "ready"


def test_prune_retired_fallbacks_handles_missing_or_invalid_structure() -> None:
    # Missing provider_recovery
    assert _prune_retired_fallbacks({}) == {}

    # Invalid fallbacks type (not a sequence or is a string)
    policy_str = {"provider_recovery": {"fallbacks": "invalid_string"}}
    assert _prune_retired_fallbacks(policy_str) == policy_str

    policy_int = {"provider_recovery": {"fallbacks": 123}}
    assert _prune_retired_fallbacks(policy_int) == policy_int

    # fallbacks list containing non-mapping elements or mappings without valid agent
    policy_mixed = {
        "provider_recovery": {
            "fallbacks": [
                None,
                "not_a_dict",
                123,
                {"agent": None},
                {"agent": "gemini", "model": "gemini-1.5-pro"},
                {"agent": "codex", "model": "gpt-5.5"},
            ]
        }
    }
    pruned = _prune_retired_fallbacks(policy_mixed)
    fallbacks = pruned["provider_recovery"]["fallbacks"]
    assert fallbacks == [
        None,
        None,
        None,
        None,
        None,
        {"agent": "codex", "model": "gpt-5.5"},
    ]


async def test_retry_promotes_launchable_fallback_when_primary_retired(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        first.agent = "gemini"
        first.task_policy = {
            **first.task_policy,
            "provider_recovery": {
                "fallbacks": [
                    {"agent": "gemini-old", "model": "gemini-1.0"},
                    {"agent": "codex", "model": "gpt-5.5"},
                ]
            },
        }
        await session.commit()
    await _mark_failed(factory, first.id)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            settings=settings,
            run_subprocess=_docker_ok,
        )

    assert retry.new_workspace.agent == "codex"
    retried_policy = retry.new_workspace.task_policy
    assert retried_policy.get("agent_model") == "gpt-5.5"
    fallbacks = retried_policy.get("provider_recovery", {}).get("fallbacks", [])
    assert fallbacks == [None, None]
    preflight = retried_policy["provider_readiness_preflight"]
    assert preflight["readiness_status"] == "ready"


def test_prune_and_migrate_retired_agent_promotes_fallback() -> None:
    policy = {
        "provider_recovery": {
            "fallbacks": [
                {"agent": "gemini", "model": "gemini-1.5-pro"},
                {"agent": "codex", "model": "gpt-5.5"},
                {"agent": "opencode", "model": "opencode-1"},
            ]
        }
    }
    pruned, target_agent = _prune_and_migrate_retired_agent(policy, current_agent="gemini")
    assert target_agent == "codex"
    assert pruned["agent_model"] == "gpt-5.5"
    assert pruned["provider_recovery"]["fallbacks"] == [
        None,
        None,
        {"agent": "opencode", "model": "opencode-1"},
    ]
    assert pruned["provider_recovery_state"]["fallback_attempt_number"] == 2
    assert pruned["provider_recovery_state"]["launched_fallback_attempts"] == 1
    assert pruned["provider_recovery_state"]["retry_attempt_number"] == 0


def test_prune_and_migrate_retired_agent_respects_consumed_fallback_attempt_number() -> None:
    policy = {
        "provider_recovery": {
            "fallbacks": [
                {"agent": "codex", "model": "gpt-5.5"},
                {"agent": "claude_code", "model": "claude-3-7-sonnet"},
            ]
        },
        "provider_recovery_state": {
            "fallback_attempt_number": 1,
            "launched_fallback_attempts": 1,
        },
    }
    pruned, target_agent = _prune_and_migrate_retired_agent(policy, current_agent="gemini")
    assert target_agent == "claude_code"
    assert pruned["agent_model"] == "claude-3-7-sonnet"
    assert pruned["provider_recovery"]["fallbacks"] == [
        {"agent": "codex", "model": "gpt-5.5"},
        None,
    ]
    assert pruned["provider_recovery_state"]["fallback_attempt_number"] == 2
    assert pruned["provider_recovery_state"]["launched_fallback_attempts"] == 2
    assert pruned["provider_recovery_state"]["retry_attempt_number"] == 0


def test_prune_and_migrate_retired_agent_respects_max_fallback_attempts_exhaustion() -> None:
    policy = {
        "provider_recovery": {
            "max_fallback_attempts": 0,
            "fallbacks": [
                {"agent": "codex", "model": "gpt-5.5"},
                {"agent": "claude_code", "model": "claude-3-7-sonnet"},
            ],
        },
        "provider_recovery_state": {
            "fallback_attempt_number": 0,
            "launched_fallback_attempts": 0,
        },
    }
    pruned, target_agent = _prune_and_migrate_retired_agent(policy, current_agent="gemini")
    assert target_agent == "gemini"
    assert "agent_model" not in pruned
    assert pruned["provider_recovery"]["fallbacks"] == [
        {"agent": "codex", "model": "gpt-5.5"},
        {"agent": "claude_code", "model": "claude-3-7-sonnet"},
    ]


def test_prune_and_migrate_retired_agent_advances_recovery_cursor_and_launched_count() -> None:
    policy = {
        "provider_recovery": {
            "max_fallback_attempts": 1,
            "fallbacks": [
                {"agent": "codex", "model": "gpt-5.5"},
                {"agent": "claude_code", "model": "claude-3-7-sonnet"},
            ],
        },
        "provider_recovery_state": {
            "fallback_attempt_number": 0,
            "launched_fallback_attempts": 0,
        },
    }
    pruned, target_agent = _prune_and_migrate_retired_agent(policy, current_agent="gemini")
    assert target_agent == "codex"
    assert pruned["agent_model"] == "gpt-5.5"
    assert pruned["provider_recovery_state"]["fallback_attempt_number"] == 1
    assert pruned["provider_recovery_state"]["launched_fallback_attempts"] == 1
    assert pruned["provider_recovery_state"]["retry_attempt_number"] == 0

    from awf.service.provider_recovery import (
        _select_fallback_target_with_index,
        parse_provider_recovery_policy,
        parse_provider_recovery_state,
    )

    rec_policy = parse_provider_recovery_policy(pruned)
    rec_state = parse_provider_recovery_state(pruned)
    fb_target, fb_idx = _select_fallback_target_with_index(rec_policy, rec_state)
    assert fb_target is None
