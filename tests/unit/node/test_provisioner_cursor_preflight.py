"""Unit tests for provision-time deferred Cursor Auto Router preflight."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.node.provisioner_cursor_preflight import ProvisionerCursorPreflightMixin
from awf.node.provisioner_helpers import _PROFILE_TRUSTED_BASE_SHA_KEY
from awf.profiles.models import WorkspaceProfile


class _Harness(ProvisionerCursorPreflightMixin):
    def __init__(self) -> None:
        self._session_factory = None
        self.mark_failed = AsyncMock()
        self.stale_skips: list[str] = []

    async def _mark_failed(self, **kwargs: Any) -> None:
        await self.mark_failed(**kwargs)

    async def _record_stale_action_skip(self, *_args: Any, **kwargs: Any) -> None:
        self.stale_skips.append(str(kwargs.get("action")))


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_marks_failed_when_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight_profiles: list[dict[str, object]] = []

    async def _blocked(**kwargs: object) -> dict[str, object]:
        preflight_profiles.append(dict(kwargs["resolved_profile"]))
        return {
            "blocks_launch": True,
            "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            "message": "Router is unavailable.",
        }

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _blocked,
    )
    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        resolved_profile=None,
        execution_claim_epoch=3,
        task_policy={
            "cursor_auto_mode": "intelligence",
            "pr_adoption": {"base_sha": "a" * 40},
        },
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=None,
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "repo-local",
            "runtime": {"environment": {"CURSOR_API_KEY": "cursor-profile-secret"}},
        }
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=profile,
        execution_claim_epoch=3,
        trusted_base_profile_sha="a" * 40,
    )
    assert stopped is True
    assert preflight_profiles[0]["runtime"]["environment"] == {
        "CURSOR_API_KEY": "cursor-profile-secret"
    }
    assert persisted.resolved_profile["runtime"]["environment"] == {
        "CURSOR_API_KEY": REDACTION_MARKER
    }
    assert ws.resolved_profile == persisted.resolved_profile
    assert persisted.task_policy["pr_adoption"][_PROFILE_TRUSTED_BASE_SHA_KEY] == "a" * 40
    assert persisted.task_policy["provider_readiness_preflight"] == {
        "blocks_launch": True,
        "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
        "message": "Router is unavailable.",
    }
    assert ws.task_policy["provider_readiness_preflight"]["blocks_launch"] is True
    session.commit.assert_awaited_once()
    harness.mark_failed.assert_awaited_once()
    kwargs = harness.mark_failed.await_args.kwargs
    assert kwargs["failure_reason"] is FailureReason.policy_failure
    assert kwargs["reason_code"] == "CURSOR_ROUTER_UNAVAILABLE"
    assert kwargs["from_status"] is WorkspaceStatus.provisioning
    assert kwargs["execution_claim_epoch"] == 3
    assert kwargs["trusted_base_profile_sha"] == "a" * 40
    assert kwargs["event_payload"]["provider_readiness_preflight"]["blocks_launch"] is True


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_blocking_replaces_legacy_freeze_and_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking path must replace a legacy PR-head freeze and stamp trusted-base provenance."""

    async def _blocked(**kwargs: object) -> dict[str, object]:
        return {
            "blocks_launch": True,
            "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            "message": "Router is unavailable.",
        }

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _blocked,
    )
    trusted_sha = "b" * 40
    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        resolved_profile={
            "name": "legacy-head",
            "source": "repo:.awf/workspace.yml",
        },
        execution_claim_epoch=3,
        task_policy={
            "cursor_auto_mode": "intelligence",
            "pr_adoption": {"base_sha": trusted_sha},
        },
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=persisted.resolved_profile,
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "base-safe",
            "source": "repo:.awf/workspace.yml",
            "monitor": {"auto_merge": {"default": True}},
        }
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=profile,
        execution_claim_epoch=3,
        trusted_base_profile_sha=trusted_sha,
    )
    assert stopped is True
    assert persisted.resolved_profile["name"] == "base-safe"
    assert ws.resolved_profile["name"] == "base-safe"
    assert persisted.task_policy["pr_adoption"][_PROFILE_TRUSTED_BASE_SHA_KEY] == trusted_sha
    harness.mark_failed.assert_awaited_once()
    assert harness.mark_failed.await_args.kwargs["trusted_base_profile_sha"] == trusted_sha


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_skips_profile_publish_when_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded claim must not write resolved_profile into the new claimant."""

    async def _blocked(**_kwargs: object) -> dict[str, object]:
        return {
            "blocks_launch": True,
            "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            "message": "Router is unavailable.",
        }

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _blocked,
    )
    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        resolved_profile=None,
        execution_claim_epoch=9,
        task_policy={},
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=None,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=WorkspaceProfile(name="repo-local"),
        execution_claim_epoch=3,
    )
    assert stopped is True
    assert persisted.resolved_profile is None
    assert ws.resolved_profile is None
    assert "provider_readiness_preflight" not in persisted.task_policy
    assert "provider_readiness_preflight" not in ws.task_policy
    session.commit.assert_awaited_once()
    harness.mark_failed.assert_awaited_once()


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_noop_when_not_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _skip(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _skip,
    )
    harness = _Harness()
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=SimpleNamespace(agent="cursor", task_policy={}),  # type: ignore[arg-type]
        profile=WorkspaceProfile(name="repo-local"),
    )
    assert stopped is False
    harness.mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_stops_on_prior_blocking_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reclaim after a committed blocking write must not skip the Router gate.

    The blocking path may persist ``provider_readiness_preflight`` with
    ``blocks_launch: True`` before ``_mark_failed`` commits. If that fail
    transition never lands, a later provision reclaim must not treat the
    snapshot as a passed gate and continue into stack launch.
    """

    async def _skip(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _skip,
    )
    harness = _Harness()
    blocking = {
        "blocks_launch": True,
        "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
        "message": "Router is unavailable.",
    }
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=SimpleNamespace(  # type: ignore[arg-type]
            agent="cursor",
            task_policy={
                "cursor_auto_mode": "intelligence",
                "provider_readiness_preflight": blocking,
            },
        ),
        profile=WorkspaceProfile(name="repo-local"),
        execution_claim_epoch=3,
    )
    assert stopped is True
    harness.mark_failed.assert_awaited_once()
    kwargs = harness.mark_failed.await_args.kwargs
    assert kwargs["failure_reason"] is FailureReason.policy_failure
    assert kwargs["reason_code"] == "CURSOR_ROUTER_UNAVAILABLE"
    assert kwargs["from_status"] is WorkspaceStatus.provisioning
    assert kwargs["execution_claim_epoch"] == 3
    assert kwargs["event_payload"]["provider_readiness_preflight"] == blocking


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_persists_ready_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ready(**_kwargs: object) -> dict[str, object]:
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _ready,
    )
    recorded: list[dict[str, object]] = []

    async def _record(_repo: object, workspace: object, preflight: object) -> None:
        recorded.append({"workspace": workspace, "preflight": dict(preflight)})  # type: ignore[arg-type]

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight._record_provider_readiness_preflight",
        _record,
    )

    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        execution_claim_epoch=3,
        resolved_profile=None,
        task_policy={"cursor_auto_mode": "intelligence"},
    )
    repo = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("ready path must use get_for_update")),
        get_for_update=AsyncMock(return_value=persisted),
    )
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    profile = WorkspaceProfile(name="repo-local")
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=None,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=profile,
        execution_claim_epoch=3,
    )
    assert stopped is False
    harness.mark_failed.assert_not_awaited()
    repo.get_for_update.assert_awaited_once_with("ws_test")
    repo.get.assert_not_awaited()
    # Ready path must not publish resolved_profile: host-port admission owns
    # that write under the advisory lock (first-committer-wins). Publishing
    # here races concurrent auto-profile provisioners.
    assert persisted.resolved_profile is None
    assert ws.resolved_profile is None
    assert persisted.task_policy["provider_readiness_preflight"] == {
        "blocks_launch": False,
        "reason_code": "CURSOR_ROUTER_AVAILABLE",
    }
    assert ws.task_policy["provider_readiness_preflight"]["reason_code"] == (
        "CURSOR_ROUTER_AVAILABLE"
    )
    assert recorded[0]["preflight"]["reason_code"] == "CURSOR_ROUTER_AVAILABLE"
    session.commit.assert_awaited_once()


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_ready_does_not_publish_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6bVPJV: ready path must not commit ports early."""

    async def _ready(**_kwargs: object) -> dict[str, object]:
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _ready,
    )
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight._record_provider_readiness_preflight",
        AsyncMock(),
    )
    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        execution_claim_epoch=1,
        resolved_profile=None,
        task_policy={"cursor_auto_mode": "intelligence"},
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=None,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=WorkspaceProfile(name="repo-local"),
        execution_claim_epoch=1,
    )
    assert stopped is False
    assert persisted.resolved_profile is None
    assert ws.resolved_profile is None
    session.commit.assert_awaited_once()


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_skips_ready_write_when_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded claim must not commit readiness into the new claimant's timeline."""

    async def _ready(**_kwargs: object) -> dict[str, object]:
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _ready,
    )
    recorded: list[dict[str, object]] = []

    async def _record(_repo: object, workspace: object, preflight: object) -> None:
        recorded.append({"workspace": workspace, "preflight": dict(preflight)})  # type: ignore[arg-type]

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight._record_provider_readiness_preflight",
        _record,
    )

    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        execution_claim_epoch=9,
        resolved_profile=None,
        task_policy={"cursor_auto_mode": "intelligence"},
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=None,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=WorkspaceProfile(name="repo-local"),
        execution_claim_epoch=3,
    )
    assert stopped is True
    repo.get_for_update.assert_awaited_once_with("ws_test")
    assert persisted.resolved_profile is None
    assert ws.resolved_profile is None
    assert "provider_readiness_preflight" not in persisted.task_policy
    assert "provider_readiness_preflight" not in ws.task_policy
    assert recorded == []
    session.commit.assert_not_awaited()
    harness.mark_failed.assert_not_awaited()
    assert harness.stale_skips == []


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_ready_leaves_existing_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ready path must not touch an already-persisted resolved_profile."""

    async def _ready(**_kwargs: object) -> dict[str, object]:
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _ready,
    )

    async def _record(_repo: object, workspace: object, preflight: object) -> None:
        return None

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight._record_provider_readiness_preflight",
        _record,
    )

    existing = {"name": "already-persisted"}
    persisted = SimpleNamespace(
        status=WorkspaceStatus.provisioning.value,
        execution_claim_epoch=3,
        resolved_profile=existing,
        task_policy={"cursor_auto_mode": "intelligence"},
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    ws = SimpleNamespace(
        agent="cursor",
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile=existing,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=ws,  # type: ignore[arg-type]
        profile=WorkspaceProfile(name="repo-local"),
        execution_claim_epoch=3,
    )
    assert stopped is False
    assert persisted.resolved_profile is existing
    assert ws.resolved_profile is existing
    session.commit.assert_awaited_once()


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_stops_on_stale_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ready(**_kwargs: object) -> dict[str, object]:
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _ready,
    )
    persisted = SimpleNamespace(
        status=WorkspaceStatus.cancelled.value,
        task_policy={"cursor_auto_mode": "intelligence"},
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=persisted))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_test",
        ws=SimpleNamespace(  # type: ignore[arg-type]
            agent="cursor",
            task_policy={"cursor_auto_mode": "intelligence"},
        ),
        profile=WorkspaceProfile(name="repo-local"),
    )
    assert stopped is True
    repo.get_for_update.assert_awaited_once_with("ws_test")
    assert harness.stale_skips == ["deferred_cursor_router_preflight"]
    session.commit.assert_awaited_once()


@pytest.mark.unit
async def test_provisioner_deferred_cursor_preflight_stops_when_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel/destroy may remove the row during the Router probe; do not continue."""

    async def _ready(**_kwargs: object) -> dict[str, object]:
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.run_deferred_cursor_auto_mode_provider_preflight",
        _ready,
    )
    repo = SimpleNamespace(get_for_update=AsyncMock(return_value=None))
    session = SimpleNamespace(commit=AsyncMock())

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_exc: object) -> None:
            return None

    harness = _Harness()
    harness._session_factory = lambda: _SessionCtx()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "awf.node.provisioner_cursor_preflight.WorkspaceRepository",
        lambda _session: repo,
    )
    stopped = await harness._run_deferred_cursor_auto_router_preflight(
        workspace_id="ws_gone",
        ws=SimpleNamespace(  # type: ignore[arg-type]
            agent="cursor",
            task_policy={"cursor_auto_mode": "intelligence"},
        ),
        profile=WorkspaceProfile(name="repo-local"),
    )
    assert stopped is True
    repo.get_for_update.assert_awaited_once_with("ws_gone")
    session.commit.assert_not_awaited()
