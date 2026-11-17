"""Hosted authority grace tests (part 002)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError

from awf.common.commands import FakeCommandRunner
from awf.common.workspace_scope import without_authority_grace_wait
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import (
    MaterialDeniedError,
    MaterialLapsedError,
    WorkspaceAuthorityDeferredError,
    WorkspaceScopeDeniedError,
)
from awf.hosted_node.runtime_authority import AuthorizedMaterials
from awf.hosted_node.runtime_consumers import HostedCommandRunner
from awf.hosted_node.schemas import Binding
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_authority_grace_parts.helpers import (
    _mock_consumers,
)


@pytest.mark.parametrize(
    "error", [TimeoutError(), OSError("offline"), OperationalError("db", {}, None)]
)
@pytest.mark.parametrize("place", ["scope", "watcher", "sweep"])
async def test_single_transient_then_success_does_not_stop(tmp_path, error, place):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    if place == "scope":
        authority.workspace.side_effect = [error, material, material]
        async with scope.scope("ws_exact"):
            pass
        scope.stop_workspace.assert_not_awaited()
    elif place == "watcher":
        calls = {"n": 0}

        async def workspace(_workspace_id: str):
            calls["n"] += 1
            if calls["n"] == 2:
                raise error
            return material

        authority.workspace.side_effect = workspace
        async with scope.scope("ws_exact"):
            await asyncio.sleep(0.05)
        scope.stop_workspace.assert_not_awaited()
    else:
        authority.admission = AsyncMock()
        authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
        authority.reconcile_workspace = AsyncMock(side_effect=[error, True])
        assert await scope.admission_check()
        scope.stop_workspace.assert_not_awaited()
        assert await scope.admission_check()
        scope.stop_workspace.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [TimeoutError(), OSError("offline"), OperationalError("db", {}, None)]
)
@pytest.mark.parametrize("place", ["scope", "watcher", "sweep"])
async def test_transient_failures_past_window_stop_unavailable(tmp_path, monkeypatch, error, place):
    scope, authority = _mock_consumers(tmp_path, grace=1)
    material = authority.workspace.return_value
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    if place == "scope":

        async def advance_on_sleep(seconds: float) -> None:
            clock["now"] = origin + timedelta(seconds=1)

        authority.workspace.side_effect = error
        monkeypatch.setattr(
            "awf.hosted_node.runtime_consumers.asyncio.sleep",
            advance_on_sleep,
        )
        with pytest.raises(type(error)):
            async with scope.scope("ws_exact"):
                pytest.fail("exhausted must not start")
        assert (
            scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"
        )
    elif place == "watcher":
        authority.workspace.side_effect = [material, error, error, error]

        async def advance() -> None:
            await asyncio.sleep(0.02)
            clock["now"] = origin + timedelta(seconds=1)
            await asyncio.sleep(10)

        with pytest.raises((WorkspaceScopeDeniedError, type(error), asyncio.CancelledError)):
            async with scope.scope("ws_exact"):
                await advance()
        assert any(
            call.kwargs.get("reason_code") == "HOSTED_AUTHORITY_UNAVAILABLE"
            for call in scope.stop_workspace.await_args_list
        )
    else:
        authority.admission = AsyncMock()
        authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
        authority.reconcile_workspace = AsyncMock(side_effect=error)
        assert await scope.admission_check()
        scope.stop_workspace.assert_not_awaited()
        clock["now"] = origin + timedelta(seconds=1)
        assert await scope.admission_check()
        assert (
            scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"
        )


@pytest.mark.parametrize("place", ["scope", "watcher", "sweep"])
async def test_grace_zero_matches_today_stop_on_lapse(tmp_path, place):
    scope, authority = _mock_consumers(tmp_path, grace=0)
    material = authority.workspace.return_value
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    if place == "scope":
        authority.workspace.side_effect = lapse
        with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
            async with scope.scope("ws_exact"):
                pytest.fail("grace 0 must stop")
        scope.stop_workspace.assert_awaited_once_with("ws_exact")
    elif place == "watcher":
        calls = {"n": 0}

        async def workspace(_workspace_id: str):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise lapse
            return material

        authority.workspace.side_effect = workspace
        with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
            async with scope.scope("ws_exact"):
                await asyncio.sleep(10)
        scope.stop_workspace.assert_awaited_once_with("ws_exact")
    else:
        authority.admission = AsyncMock()
        authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
        authority.reconcile_workspace = AsyncMock(side_effect=lapse)
        assert await scope.admission_check()
        scope.stop_workspace.assert_awaited_once_with("ws_exact")


async def test_lapse_after_refresh_preserves_mounted_view(bound_workspace, monkeypatch):
    """A grace-eligible lapse after refresh must not strip Docker-mounted credentials.

    If material expires between a successful view refresh and the final checks,
    MaterialLapsedError must still reject new credential access, but the already
    mounted view must remain so consumers can defer without orphaning the mount.
    """
    runtime, value, _ = bound_workspace
    await runtime.workspace("ws_cloud_stable")
    view = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    before = {name: (view / name).read_bytes() for name in ("openai.api-key", "github.api-key")}
    expiry = Binding.model_validate(value["hosted"]["credentials"]["agent"]).expires_at
    original_publish = runtime.views.publish
    original_refresh = runtime.views.refresh

    def _lapse_after(write):
        def wrapped(*args, **kwargs):
            published = write(*args, **kwargs)
            now = expiry + timedelta(seconds=1)

            class Clock(datetime):
                @classmethod
                def now(cls, tz: tzinfo | None = None) -> Self:
                    return cls.fromtimestamp(now.timestamp(), tz=tz)

            monkeypatch.setattr("awf.hosted_node.runtime_authority.datetime", Clock)
            monkeypatch.setattr("awf.hosted_node.receiver.datetime", Clock)
            return published

        return wrapped

    monkeypatch.setattr(runtime.views, "publish", _lapse_after(original_publish))
    monkeypatch.setattr(runtime.views, "refresh", _lapse_after(original_refresh))
    with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
        await runtime.workspace("ws_cloud_stable")
    assert {
        name: (view / name).read_bytes() for name in ("openai.api-key", "github.api-key")
    } == before
    assert view.is_dir()


@pytest.mark.parametrize(
    "error",
    [TimeoutError("db timeout"), OSError("offline"), OperationalError("db", {}, None)],
)
async def test_transient_after_refresh_preserves_mounted_view(bound_workspace, monkeypatch, error):
    """Grace-eligible transient failures after refresh must keep the mounted view.

    Watcher/sweep defer for TimeoutError, OSError, and SQLAlchemyError the same
    way they defer MaterialLapsedError. Retracting the publication on those
    failures would unlink credentials that a still-running container cannot see
    again when a later publication lands in a new directory.
    """
    runtime, _, _ = bound_workspace
    await runtime.workspace("ws_cloud_stable")
    view = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    before = {name: (view / name).read_bytes() for name in ("openai.api-key", "github.api-key")}
    original_publish = runtime.views.publish
    original_refresh = runtime.views.refresh

    def _fail_after(write):
        def wrapped(*args, **kwargs):
            published = write(*args, **kwargs)

            async def boom(*_args, **_kwargs):
                raise error

            monkeypatch.setattr(runtime, "_snapshot_status", boom)
            return published

        return wrapped

    monkeypatch.setattr(runtime.views, "publish", _fail_after(original_publish))
    monkeypatch.setattr(runtime.views, "refresh", _fail_after(original_refresh))
    with pytest.raises(type(error)):
        await runtime.workspace("ws_cloud_stable")
    assert {
        name: (view / name).read_bytes() for name in ("openai.api-key", "github.api-key")
    } == before
    assert view.is_dir()


def test_hosted_authority_grace_seconds_default_and_zero(tmp_path):
    base = {
        "owner": "owner",
        "incarnation": "123",
        "recovery_generation": 1,
        "controller_token": "synthetic-controller",
        "material_root": tmp_path,
    }
    assert HostedSettings(**base).hosted_authority_grace_seconds == 600
    assert (
        HostedSettings(**base, hosted_authority_grace_seconds=0).hosted_authority_grace_seconds == 0
    )


async def test_run_during_lapse_blocks_then_uses_renewed_material(tmp_path):
    """Command prepare must wait out a lapse and then inject renewed credentials."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    entry = authority.workspace.return_value
    lapsed = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    renewed = AuthorizedMaterials(
        {"GH_TOKEN": "renewed", "GITHUB_TOKEN": "renewed", "CODEX_API_KEY": "renewed-agent"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return entry
        if calls["n"] == 2:
            raise lapsed
        return renewed

    authority.workspace.side_effect = workspace
    delegate = FakeCommandRunner()
    runner = HostedCommandRunner(scope, delegate)
    async with scope.scope("ws_exact"):
        await runner.run(["gh", "pr", "view"])
    assert delegate.calls
    assert delegate.calls[0].env["GH_TOKEN"] == "renewed"
    assert "ws_exact" not in scope._authority_grace


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), OSError("offline"), OperationalError("db", {}, None)],
)
async def test_run_during_transient_blocks_then_uses_renewed_material(tmp_path, error):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    entry = authority.workspace.return_value
    renewed = AuthorizedMaterials(
        {"GH_TOKEN": "renewed", "GITHUB_TOKEN": "renewed", "CODEX_API_KEY": "renewed-agent"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return entry
        if calls["n"] == 2:
            raise error
        return renewed

    authority.workspace.side_effect = workspace
    delegate = FakeCommandRunner()
    async with scope.scope("ws_exact"):
        await HostedCommandRunner(scope, delegate).run(["git", "fetch", "origin"])
    assert delegate.calls[0].env["GH_TOKEN"] == "renewed"
    assert "ws_exact" not in scope._authority_grace


async def test_run_exhausts_grace_and_raises(tmp_path, monkeypatch):
    scope, authority = _mock_consumers(tmp_path, grace=1)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        clock["now"] = origin + timedelta(seconds=1)
        raise lapse

    authority.workspace.side_effect = workspace
    delegate = FakeCommandRunner()
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
                await HostedCommandRunner(scope, delegate).run(["gh", "pr", "checks"])
    assert not delegate.calls


async def test_in_scope_grace_exhaustion_stops_once(tmp_path, monkeypatch):
    """Early HOSTED_AUTHORITY_UNAVAILABLE stop must not be repeated by the finalizer."""
    scope, authority = _mock_consumers(tmp_path, grace=1)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=2))
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        clock["now"] = origin + timedelta(seconds=1)
        raise lapse

    authority.workspace.side_effect = workspace
    delegate = FakeCommandRunner()
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
                await HostedCommandRunner(scope, delegate).run(["gh", "pr", "checks"])
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.args == ("ws_exact",)
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"
    assert not delegate.calls


async def test_run_revocation_raises_at_once(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise MaterialDeniedError("GRANT_REVOKED")

    authority.workspace.side_effect = workspace
    delegate = FakeCommandRunner()
    with pytest.raises(WorkspaceScopeDeniedError, match="GRANT_REVOKED"):
        async with scope.scope("ws_exact"):
            with pytest.raises(MaterialDeniedError, match="GRANT_REVOKED"):
                await HostedCommandRunner(scope, delegate).run(["gh", "pr", "create"])
    assert not delegate.calls
    assert "ws_exact" not in scope._authority_grace


async def test_run_grace_zero_raises_lapse_immediately(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=0)
    material = authority.workspace.return_value
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise lapse

    authority.workspace.side_effect = workspace
    delegate = FakeCommandRunner()
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
                await HostedCommandRunner(scope, delegate).run(["git", "status"])
    assert not delegate.calls


async def test_environment_opt_out_raises_deferred_without_waiting(tmp_path, monkeypatch):
    """Poll-inline recovery must not sleep in environment() during command prepare.

    Scope entry can succeed, then authority lapses before HostedCommandRunner
    prepares the PR-lookup command. without_authority_grace_wait must still
    convert a grace-eligible failure into WorkspaceAuthorityDeferredError so the
    shared worker poll is not blocked for the remaining grace window.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    material = authority.workspace.return_value
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    slept: list[float] = []
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise lapse

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise AssertionError("environment must not sleep under grace opt-out")

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    async with scope.scope("ws_exact"):
        with (
            without_authority_grace_wait(),
            pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"),
        ):
            await scope.environment()
    assert slept == []
    scope.stop_workspace.assert_not_awaited()


async def test_hosted_command_prepare_opt_out_raises_deferred_without_waiting(
    tmp_path, monkeypatch
):
    """BranchOpenPullRequestResolver uses HostedCommandRunner; prepare must honor opt-out.

    After scope entry succeeds, a lapse during command prepare must raise
    WorkspaceAuthorityDeferredError instead of sleeping the remaining grace
    window — otherwise preserved-recovery blocks the shared worker poll.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    material = authority.workspace.return_value
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    slept: list[float] = []
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise lapse

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise AssertionError("HostedCommandRunner prepare must not sleep under grace opt-out")

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    delegate = FakeCommandRunner()
    async with scope.scope("ws_exact"):
        with (
            without_authority_grace_wait(),
            pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"),
        ):
            await HostedCommandRunner(scope, delegate).run(["gh", "pr", "list"])
    assert slept == []
    assert not delegate.calls
    scope.stop_workspace.assert_not_awaited()


async def test_git_environment_waits_out_authority_lapse(tmp_path):
    """git_environment waits out a brief lapse wherever it is called.

    Including under a shared git mirror lock (operator-accepted trade-off):
    failing or deferring a git step there fails the workspace; the wait is
    bounded by hosted_authority_grace_seconds.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    entry = authority.workspace.return_value
    renewed = AuthorizedMaterials(
        {"GH_TOKEN": "renewed", "GITHUB_TOKEN": "renewed", "CODEX_API_KEY": "renewed-agent"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return entry
        if calls["n"] == 2:
            raise lapse
        return renewed

    authority.workspace.side_effect = workspace
    async with scope.scope("ws_exact"):
        env = await scope.git_environment()
    assert env["GH_TOKEN"] == "renewed"
    assert "ws_exact" not in scope._authority_grace


async def test_stack_profile_mounts_waits_out_authority_lapse(tmp_path):
    """Stack prepare must wait a brief lapse instead of failing provisioning.

    After scope entry succeeds, ComposeStackLauncher calls consumer
    profile_mounts during _compose_spec. A renewal gap must use the same
    grace window as environment()/scope entry, not mark the workspace failed.
    """
    from awf.node.secret_mounts import LocalSecretLeaseResolution
    from awf.profiles.models import WorkspaceProfile

    scope, authority = _mock_consumers(tmp_path, grace=600)
    renewed = LocalSecretLeaseResolution(mounts=(), metadata={"phase": "renewed"})
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def profile_mounts(_profile: WorkspaceProfile, _workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lapse
        return renewed

    authority.profile_mounts = AsyncMock(side_effect=profile_mounts)
    result = await scope.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_exact")
    assert result.metadata == {"phase": "renewed"}
    assert calls["n"] == 2
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()


async def test_stack_resource_limits_waits_out_authority_lapse(tmp_path):
    """resource_limits during stack prepare must honor the same grace wait."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def resource_limits(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lapse
        return (4.0, 8.0)

    authority.resource_limits = AsyncMock(side_effect=resource_limits)
    assert await scope.resource_limits("ws_exact") == (4.0, 8.0)
    assert calls["n"] == 2
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()


async def test_watcher_workspace_success_does_not_reset_resource_limits_grace(
    tmp_path, monkeypatch
):
    """Healthy material polls must not reset a failing reservation grace window.

    Scope watcher clears grace on successful workspace() checks. When
    resource_limits() keeps timing out during stack prepare, those clears used
    to reopen a full allowance on every poll so provisioning never exhausted
    hosted_authority_grace_seconds (PRRT_kwDOSJAM6s64aCNM).
    """
    scope, authority = _mock_consumers(tmp_path, grace=3)
    scope.poll_seconds = 1.0
    origin = datetime.now(UTC)
    clock = {"now": origin}
    limits_calls = {"n": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    async def resource_limits(_workspace_id: str):
        limits_calls["n"] += 1
        # Simulate wall time advancing across reservation timeouts while the
        # watcher keeps confirming healthy material on each poll cycle.
        clock["now"] = origin + timedelta(seconds=limits_calls["n"])
        # Same clear the watcher performs after a successful workspace() poll.
        scope._clear_authority_grace("ws_exact", checker="watcher")
        raise TimeoutError()

    authority.resource_limits = AsyncMock(side_effect=resource_limits)

    with pytest.raises(TimeoutError):
        await scope.resource_limits("ws_exact")

    assert limits_calls["n"] >= 3
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"


async def test_environment_cancel_during_transient_keeps_shared_workspace_clock(
    tmp_path, monkeypatch
) -> None:
    """Cancel during command_prepare clears only that window, not watcher.

    Independent checkers: cancelling ``environment()`` releases
    ``command_prepare`` without touching an open watcher deadline
    (PRRT_kwDOSJAM6s64a22J under per-checker clocks).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    waiting = asyncio.Event()
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise TimeoutError()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    # Seed an independent watcher window that must survive command_prepare cancel.
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="watcher") == "defer"
    )
    watcher_started = scope._authority_grace["ws_exact"]["watcher"].started_at

    async with scope.scope("ws_exact"):
        task = asyncio.create_task(scope.environment())
        await asyncio.wait_for(waiting.wait(), 1)
        assert "command_prepare" in scope._authority_grace["ws_exact"]
        assert "watcher" in scope._authority_grace["ws_exact"]
        clock["now"] = origin + timedelta(seconds=100)
        task.cancel("operator cancel during grace wait")
        with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
            await task
        assert "command_prepare" not in scope._authority_grace.get("ws_exact", {})
        assert scope._authority_grace["ws_exact"]["watcher"].started_at == watcher_started

        clock["now"] = origin + timedelta(seconds=300)
        with structlog.testing.capture_logs() as logs:
            assert (
                scope._classify_authority_failure("ws_exact", TimeoutError(), checker="watcher")
                == "defer"
            )
        assert scope._authority_grace["ws_exact"]["watcher"].started_at == watcher_started
        assert not any(log["event"] == "hosted_node.authority_grace_started" for log in logs)
        authority.workspace.side_effect = None
        authority.workspace.return_value = material
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace


async def test_nested_scope_cancel_during_transient_keeps_shared_workspace_clock(
    tmp_path, monkeypatch
) -> None:
    """Cancel during nested_entry clears only that window, not watcher.

    Nested same-workspace re-entry uses ``nested_entry``. Clearing it on cancel
    must not reset an independent watcher deadline (PRRT_kwDOSJAM6s64a22J).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    waiting = asyncio.Event()
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise TimeoutError()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="watcher") == "defer"
    )
    watcher_started = scope._authority_grace["ws_exact"]["watcher"].started_at

    async with scope.scope("ws_exact"):
        task = asyncio.create_task(scope._scope("ws_exact").__aenter__())
        await asyncio.wait_for(waiting.wait(), 1)
        assert "nested_entry" in scope._authority_grace["ws_exact"]
        clock["now"] = origin + timedelta(seconds=100)
        task.cancel("operator cancel during nested grace wait")
        with pytest.raises(
            asyncio.CancelledError, match="operator cancel during nested grace wait"
        ):
            await task
        assert "nested_entry" not in scope._authority_grace.get("ws_exact", {})
        assert scope._authority_grace["ws_exact"]["watcher"].started_at == watcher_started

        clock["now"] = origin + timedelta(seconds=300)
        with structlog.testing.capture_logs() as logs:
            assert (
                scope._classify_authority_failure("ws_exact", TimeoutError(), checker="watcher")
                == "defer"
            )
        assert scope._authority_grace["ws_exact"]["watcher"].started_at == watcher_started
        assert not any(log["event"] == "hosted_node.authority_grace_started" for log in logs)
        authority.workspace.side_effect = None
        authority.workspace.return_value = material
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace


async def test_stack_resource_limits_cancel_releases_claim_so_later_lapse_defers(
    tmp_path, monkeypatch
):
    """Cancel during resource_limits grace wait must not leave a leaked window.

    ``_await_with_authority_grace`` clears only the cancelled checker's window
    so a brief lapse 601s later opens a fresh reconcile window instead of
    exhausting against a leaked reservation deadline.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)
    authority.resource_limits = AsyncMock(side_effect=TimeoutError())

    task = asyncio.create_task(scope.resource_limits("ws_exact"))
    await asyncio.wait_for(waiting.wait(), 1)
    assert set(scope._authority_grace["ws_exact"]) == {"resource_limits"}
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    assert await scope.admission_check()
    assert "ws_exact" not in scope._authority_grace

    clock["now"] = origin + timedelta(seconds=601)
    fresh = MaterialLapsedError(expires_at=clock["now"] - timedelta(seconds=1))
    authority.reconcile_workspace = AsyncMock(side_effect=fresh)
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]["reconcile"]
    assert window.started_at == fresh.expires_at


async def test_stack_resource_limits_opt_out_releases_claim(tmp_path, monkeypatch):
    """Opt-out DeferredError must release only the resource_limits grace window."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    assert scope._classify_authority_failure("ws_exact", lapse, checker="watcher") == "defer"
    started_at = scope._authority_grace["ws_exact"]["watcher"].started_at

    async def record_sleep(seconds: float) -> None:
        raise AssertionError("resource_limits must not sleep under grace opt-out")

    authority.resource_limits = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    with (
        without_authority_grace_wait(),
        pytest.raises(WorkspaceAuthorityDeferredError),
    ):
        await scope.resource_limits("ws_exact")
    assert set(scope._authority_grace["ws_exact"]) == {"watcher"}
    assert scope._authority_grace["ws_exact"]["watcher"].started_at == started_at
    scope.stop_workspace.assert_not_awaited()

    scope._clear_authority_grace("ws_exact", checker="watcher")
    assert "ws_exact" not in scope._authority_grace
    clock["now"] = origin + timedelta(seconds=601)
    fresh = MaterialLapsedError(expires_at=clock["now"] - timedelta(seconds=1))
    assert scope._classify_authority_failure("ws_exact", fresh, checker="reconcile") == "defer"
    assert scope._authority_grace["ws_exact"]["reconcile"].started_at == fresh.expires_at


async def test_stack_profile_mounts_opt_out_raises_deferred_without_waiting(tmp_path, monkeypatch):
    """Poll-inline opt-out must not sleep in stack-prepare authority callbacks."""
    from awf.profiles.models import WorkspaceProfile

    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise AssertionError("stack profile_mounts must not sleep under grace opt-out")

    authority.profile_mounts = AsyncMock(side_effect=lapse)
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    with (
        without_authority_grace_wait(),
        pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"),
    ):
        await scope.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_exact")
    assert slept == []
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()


async def test_stack_profile_mounts_exhaustion_stops_unavailable_before_raise(
    tmp_path, monkeypatch
):
    """Grace exhaustion during stack prepare must stop with HOSTED_AUTHORITY_UNAVAILABLE.

    ``_await_with_authority_grace`` used to bare-raise after classifying
    ``stop_exhausted``. Provisioner's generic handler then marked the workspace
    failed with an unexpected-provisioning error before the scope finalizer's
    classified stop ran; ``stop_workspace`` skipped recording because the row
    was already terminal. Stop with the exhaustion reason before exposing the
    raw error so history keeps the documented reason (PRRT_kwDOSJAM6s64WTdF).
    """
    from awf.profiles.models import WorkspaceProfile

    scope, authority = _mock_consumers(tmp_path, grace=1)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=2))
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    async def profile_mounts(_profile: WorkspaceProfile, _workspace_id: str):
        clock["now"] = origin + timedelta(seconds=1)
        raise lapse

    authority.profile_mounts = AsyncMock(side_effect=profile_mounts)
    with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
        await scope.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_exact")
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.args == ("ws_exact",)
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"
    assert "1" in scope.stop_workspace.await_args.kwargs["message"]
