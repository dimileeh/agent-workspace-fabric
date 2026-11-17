"""Hosted authority grace tests (part 003)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from awf.hosted_node.identity import (
    MaterialDeniedError,
    MaterialLapsedError,
)
from awf.hosted_node.profile_secrets import (
    PROTECTED_DECLARATION_MISMATCH,
    PROTECTED_DECLARATION_UNSUPPORTED,
    HostedProfileSecretError,
)
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_authority_grace_parts.helpers import (
    _mock_consumers,
)


async def test_in_scope_grace_stop_fences_publication_before_teardown(tmp_path, monkeypatch):
    """Early stop from environment() must set ownership.closing before teardown.

    ``_await_with_authority_grace`` stops while the outer watcher may still be
    running. association() does not treat failed as inactive, so a later
    successful watcher workspace() can republish after retract unless closing
    fences publication first (PRRT_kwDOSJAM6s64W4Hb).
    """
    from awf.hosted_node.runtime_views import owned_publication, track_publications

    scope, authority = _mock_consumers(tmp_path, grace=1)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=2))
    origin = datetime.now(UTC)
    clock = {"now": origin}
    closing_at_stop: list[bool] = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    async def workspace(_workspace_id: str):
        clock["now"] = origin + timedelta(seconds=1)
        raise lapse

    authority.workspace.side_effect = workspace

    async def stop_workspace(workspace_id: str, **_kwargs: object) -> None:
        ownership = owned_publication(scope.authority.views, workspace_id)
        closing_at_stop.append(ownership is not None and ownership.closing)

    scope.stop_workspace = AsyncMock(side_effect=stop_workspace)

    # Active outer ownership without starting the watcher: isolates the fence
    # that early stop must set before compose teardown / retract.
    with track_publications(scope.authority.views, "ws_exact"):
        token = scope.workspace_id.set("ws_exact")
        try:
            with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
                await scope.environment()
        finally:
            scope.workspace_id.reset(token)

    assert closing_at_stop == [True]
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"


async def test_stack_resource_limits_explicit_denial_stops_before_raise(tmp_path):
    """Non-lapse denials during stack prepare must stop, then re-raise."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    authority.resource_limits = AsyncMock(
        side_effect=MaterialDeniedError("HOSTED_WORKSPACE_RESERVATION_REQUIRED", 503)
    )
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_RESERVATION_REQUIRED"):
        await scope.resource_limits("ws_exact")
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_awaited_once_with("ws_exact")


@pytest.mark.parametrize(
    "reason",
    [PROTECTED_DECLARATION_UNSUPPORTED, PROTECTED_DECLARATION_MISMATCH],
)
async def test_stack_profile_mounts_profile_resolution_error_does_not_stop(tmp_path, reason: str):
    """Deterministic profile-secret errors must reach the provisioner handler.

    ``HostedProfileSecretError`` is a ``MaterialDeniedError``, so the grace
    wrapper used to classify it as ``stop_denied`` and call ``_stop()`` before
    re-raising. Production ``stop_workspace()`` then records
    ``infrastructure_failure`` / ``HOSTED_RUNTIME_STOPPED``, and the
    provisioner's ``ProfileResolutionError`` handler cannot restore
    ``PROTECTED_DECLARATION_*`` because ``_mark_failed`` skips non-provisioning
    rows (PRRT_kwDOSJAM6s64m6NJ).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    authority.profile_mounts = AsyncMock(
        side_effect=HostedProfileSecretError(reason, "local_db_password")
    )
    with pytest.raises(HostedProfileSecretError) as caught:
        await scope.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_exact")
    assert isinstance(caught.value, ProfileResolutionError)
    assert caught.value.reason == caught.value.reason_code == reason
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()


async def test_in_scope_profile_mounts_profile_resolution_error_does_not_stop(tmp_path):
    """In-scope profile-resolution failures must not record authority-loss stop.

    Scope entry succeeded; ComposeStackLauncher then hits a declaration error.
    Stopping here would leave the row failed as infrastructure before the
    provisioner can classify ``profile_resolution_failure``.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    authority.profile_mounts = AsyncMock(
        side_effect=HostedProfileSecretError(PROTECTED_DECLARATION_UNSUPPORTED, "local_db_password")
    )
    with pytest.raises(HostedProfileSecretError) as caught:
        async with scope.scope("ws_exact"):
            await scope.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_exact")
    assert caught.value.reason_code == PROTECTED_DECLARATION_UNSUPPORTED
    scope.stop_workspace.assert_not_awaited()


async def test_in_scope_stack_prepare_denial_stops_even_when_workspace_ok(tmp_path):
    """In-scope stack-prepare denials must stop before the raw error escapes.

    Production ``@workspace_consumer`` enters scope first; ComposeStackLauncher
    then calls ``resource_limits`` / ``profile_mounts``. A reservation denial
    does not make the finalizer's ``workspace()`` check fail, so skipping
    ``_stop`` for in-scope ``stop_denied`` leaves the row stuck (or lets a
    generic provisioner handler mark failed without HOSTED_RUNTIME_STOPPED).
    Stop once before re-raising; ownership.stopped keeps the finalizer quiet
    (PRRT_kwDOSJAM6s64YBYE).
    """
    from awf.hosted_node.runtime_views import owned_publication

    scope, authority = _mock_consumers(tmp_path, grace=600)
    authority.resource_limits = AsyncMock(
        side_effect=MaterialDeniedError("HOSTED_WORKSPACE_RESERVATION_REQUIRED", 503)
    )
    stop_order: list[str] = []

    async def stop_workspace(workspace_id: str, **_kwargs: object) -> None:
        ownership = owned_publication(scope.authority.views, workspace_id)
        stop_order.append("stop")
        assert ownership is not None and ownership.closing

    scope.stop_workspace = AsyncMock(side_effect=stop_workspace)

    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_RESERVATION_REQUIRED"):
        async with scope.scope("ws_exact"):
            stop_order.append("before_limits")
            try:
                await scope.resource_limits("ws_exact")
            except MaterialDeniedError:
                stop_order.append("after_limits_raise")
                raise

    assert stop_order == ["before_limits", "stop", "after_limits_raise"]
    scope.stop_workspace.assert_awaited_once_with("ws_exact")
    assert "ws_exact" not in scope._authority_grace


async def test_rule_success_clears_only_that_checker_window(tmp_path, monkeypatch):
    """Independent checkers: reconcile success does not clear resource_limits.

    Workspace-authority cohort cross-clear is covered by
    ``test_workspace_authority_success_clears_cohort_after_deferred_finalizer``.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="reconcile")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="resource_limits")
        == "defer"
    )
    limits_started = scope._authority_grace["ws_exact"]["resource_limits"].started_at

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    assert await scope.admission_check()
    assert set(scope._authority_grace["ws_exact"]) == {"resource_limits"}
    assert scope._authority_grace["ws_exact"]["resource_limits"].started_at == limits_started
    scope.stop_workspace.assert_not_awaited()


async def test_rule_wait_exit_without_stop_clears_only_own_window(tmp_path, monkeypatch):
    """Rule 2: cancel/opt-out clears only the wait's checker window."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="reconcile")
        == "defer"
    )
    reconcile_started = scope._authority_grace["ws_exact"]["reconcile"].started_at
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)
    authority.profile_mounts = AsyncMock(side_effect=TimeoutError())
    from awf.profiles.models import WorkspaceProfile

    task = asyncio.create_task(scope.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_exact"))
    await asyncio.wait_for(waiting.wait(), 1)
    assert "profile_mounts" in scope._authority_grace["ws_exact"]
    task.cancel("rule2 cancel")
    with pytest.raises(asyncio.CancelledError, match="rule2 cancel"):
        await task
    assert "profile_mounts" not in scope._authority_grace.get("ws_exact", {})
    assert scope._authority_grace["ws_exact"]["reconcile"].started_at == reconcile_started
    scope.stop_workspace.assert_not_awaited()


async def test_rule_scope_end_without_stop_drops_scope_bound_checkers(tmp_path, monkeypatch):
    """Rule 3: clean scope exit drops scope-bound windows, not reconcile."""
    scope, _authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(origin.timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="watcher") == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="command_prepare")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="reconcile")
        == "defer"
    )
    async with scope.scope("ws_exact"):
        assert "reconcile" in scope._authority_grace["ws_exact"]
    assert set(scope._authority_grace["ws_exact"]) == {"reconcile"}
    scope.stop_workspace.assert_not_awaited()


@pytest.mark.parametrize(
    "transient",
    [TimeoutError("db timeout"), OSError("offline"), OperationalError("db", {}, None)],
)
async def test_deferred_finalizer_preserves_watcher_grace_across_clean_exit(
    tmp_path, monkeypatch, transient
):
    """Deferred finalizer must not let scope exit reset the watcher grace clock.

    When the finalizer still sees a TimeoutError/OSError/SQLAlchemyError and
    defers, failure stays unset so the clean-exit path used to call
    ``_clear_scope_bound_grace`` and drop the watcher window. The next scope or
    classify then opened a fresh clock; unlike lapses, those errors have no
    ``expires_at`` backdate, so consecutive node-local errors could outlast
    ``hosted_authority_grace_seconds`` (PRRT_kwDOSJAM6s64bvKZ).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}
    material = authority.workspace.return_value
    calls = {"n": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise transient

    authority.workspace.side_effect = workspace
    async with scope.scope("ws_exact"):
        pass

    scope.stop_workspace.assert_not_awaited()
    assert set(scope._authority_grace["ws_exact"]) == {"watcher"}
    started_at = scope._authority_grace["ws_exact"]["watcher"].started_at
    assert started_at == origin

    clock["now"] = origin + timedelta(seconds=300)
    assert scope._classify_authority_failure("ws_exact", transient, checker="watcher") == "defer"
    assert scope._authority_grace["ws_exact"]["watcher"].started_at == started_at

    clock["now"] = origin + timedelta(seconds=600)
    assert (
        scope._classify_authority_failure("ws_exact", transient, checker="watcher")
        == "stop_exhausted"
    )


async def test_workspace_authority_success_clears_cohort_after_deferred_finalizer(
    tmp_path, monkeypatch
):
    """Successful authority.workspace() clears all four workspace-checker windows.

    Deferred finalizer exit keeps the watcher window (correct). Hours later a
    new scope enters successfully; that success must clear the leftover watcher
    clock (and any nested_entry / command_prepare leftovers) so a one-second
    watcher lapse starts a fresh window and defers instead of exhausting from
    the old started_at. profile_mounts / resource_limits / reconcile stay
    independent.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}
    material = authority.workspace.return_value
    calls = {"n": 0}
    transient = TimeoutError("db timeout")

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    async def workspace_first(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return material
        raise transient

    authority.workspace.side_effect = workspace_first
    async with scope.scope("ws_exact"):
        pass

    scope.stop_workspace.assert_not_awaited()
    assert set(scope._authority_grace["ws_exact"]) == {"watcher"}
    assert scope._authority_grace["ws_exact"]["watcher"].started_at == origin

    clock["now"] = origin + timedelta(seconds=601)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="nested_entry")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="command_prepare")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="reconcile")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="resource_limits")
        == "defer"
    )
    reconcile_started = scope._authority_grace["ws_exact"]["reconcile"].started_at
    limits_started = scope._authority_grace["ws_exact"]["resource_limits"].started_at

    authority.workspace.side_effect = None
    authority.workspace.return_value = material
    async with scope.scope("ws_exact"):
        assert set(scope._authority_grace["ws_exact"]) == {"reconcile", "resource_limits"}
        assert scope._authority_grace["ws_exact"]["reconcile"].started_at == reconcile_started
        assert scope._authority_grace["ws_exact"]["resource_limits"].started_at == limits_started

        clock["now"] = origin + timedelta(seconds=602)
        assert (
            scope._classify_authority_failure("ws_exact", transient, checker="watcher") == "defer"
        )
        assert scope._authority_grace["ws_exact"]["watcher"].started_at == clock["now"]
        scope.stop_workspace.assert_not_awaited()


async def test_rule_denial_or_completed_stop_clears_every_workspace_window(tmp_path):
    """Rule 4: explicit denial or authority-lost stop clears all checker windows."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="reconcile")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="resource_limits")
        == "defer"
    )
    assert set(scope._authority_grace["ws_exact"]) == {"reconcile", "resource_limits"}

    authority.resource_limits = AsyncMock(
        side_effect=MaterialDeniedError("HOSTED_WORKSPACE_RESERVATION_REQUIRED", 503)
    )
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_RESERVATION_REQUIRED"):
        await scope.resource_limits("ws_exact")
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_awaited_once_with("ws_exact")


async def test_rule_lapse_backdate_and_exhausted_window_retained_until_stop(tmp_path, monkeypatch):
    """Rule 5: lapse backdates to expires_at; exhausted window survives failed stop."""
    scope, authority = _mock_consumers(tmp_path, grace=1)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    expiry = origin - timedelta(seconds=2)
    lapse = MaterialLapsedError(expires_at=expiry)
    authority.reconcile_workspace = AsyncMock(side_effect=[TimeoutError(), lapse, lapse])
    authority.retract = AsyncMock(side_effect=[OperationalError("db", {}, None), None])

    assert await scope.admission_check()
    assert scope._authority_grace["ws_exact"]["reconcile"].started_at == origin

    clock["now"] = origin  # backdate to expiry must exhaust without advancing wall clock
    assert not await scope.admission_check()
    assert "reconcile" in scope._authority_grace["ws_exact"]
    assert scope._authority_grace["ws_exact"]["reconcile"].started_at == expiry
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"

    # Exhausted window retained until a later successful stop clears it.
    assert await scope.admission_check()
    assert "ws_exact" not in scope._authority_grace
    assert scope.stop_workspace.await_count == 2
