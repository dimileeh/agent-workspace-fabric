"""Hosted authority grace tests (part 001)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.workspace_scope import without_authority_grace_wait
from awf.hosted_node.identity import (
    MaterialDeniedError,
    MaterialLapsedError,
    WorkspaceAuthorityDeferredError,
    WorkspaceScopeDeniedError,
)
from awf.hosted_node.runtime_authority import AuthorizedMaterials
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.schemas import Binding
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_authority_grace_parts.helpers import (
    _advance_past_expiry,
    _advance_same_grant_current,
    _install_replacement_grants,
    _mock_consumers,
)
from tests.unit.test_hosted_runtime_review_retained import retained


async def test_expired_current_binding_with_healthy_grant_is_lapsed(authority, monkeypatch):
    runtime, value, _ = authority
    expiry = _advance_past_expiry(runtime, value, monkeypatch)
    with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED") as caught:
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))
    assert caught.value.status == 503
    assert caught.value.expires_at == expiry


async def test_expired_superseded_binding_is_unavailable_not_lapsed(authority, monkeypatch):
    """A replaced grant must not be classified as a benign lapse when it expires."""
    runtime, value, _ = authority
    await _install_replacement_grants(runtime, value)
    _advance_past_expiry(runtime, value, monkeypatch)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE") as caught:
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))
    assert not isinstance(caught.value, MaterialLapsedError)


async def test_same_grant_renewal_after_expired_selection_still_lapses(authority, monkeypatch):
    """Renewal that advances grant.current must not deny a selected expired predecessor.

    If refresh finishes after `_current()` selects an expired binding, the grant still
    holds preparation authority. Classifying that as UNAVAILABLE stops healthy
    workspaces; preserve LAPSED so grace recovers on the next check.
    """
    runtime, value, factory = authority
    expiry = _advance_past_expiry(runtime, value, monkeypatch)
    anchors = {
        name: Binding.model_validate(binding)
        for name, binding in value["hosted"]["credentials"].items()
    }
    selected = {name: await runtime._current(anchor) for name, anchor in anchors.items()}
    await _advance_same_grant_current(runtime, factory, selected)

    assert await runtime._holds_current_authority(selected["agent"]) is True
    assert await runtime._holds_current_authority(selected["github"]) is True
    with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED") as caught:
        await runtime._snapshot_status(selected)
    assert caught.value.expires_at == expiry


async def test_expiry_during_snapshot_read_is_lapsed_not_unavailable(authority, monkeypatch):
    """Expiry that lands inside resolve_current must still be a lapse for grace."""
    from awf.hosted_node.resolver import ProtectedResolver

    runtime, value, _ = authority
    resolve = ProtectedResolver.resolve_current
    expiry: datetime | None = None

    async def expire_mid_read(*args, **kwargs):
        nonlocal expiry
        if expiry is None:
            expiry = _advance_past_expiry(runtime, value, monkeypatch)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(ProtectedResolver, "resolve_current", expire_mid_read)
    with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED") as caught:
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))
    assert caught.value.expires_at == expiry


async def test_expiry_during_snapshot_read_of_superseded_binding_is_unavailable(
    authority, monkeypatch
):
    """Mid-read expiry of a replaced grant must still deny, not open grace."""
    from awf.hosted_node.resolver import ProtectedResolver

    runtime, value, _ = authority
    await _install_replacement_grants(runtime, value)
    resolve = ProtectedResolver.resolve_current

    async def expire_mid_read(*args, **kwargs):
        _advance_past_expiry(runtime, value, monkeypatch)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(ProtectedResolver, "resolve_current", expire_mid_read)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE") as caught:
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))
    assert not isinstance(caught.value, MaterialLapsedError)


async def test_revoke_during_snapshot_read_is_unavailable_not_lapsed(authority, monkeypatch):
    """Revocation mid-read must stay an immediate denial, not a lapse."""
    from awf.hosted_node.resolver import ProtectedResolver

    runtime, value, _ = authority
    resolve = ProtectedResolver.resolve_current
    binding = value["hosted"]["credentials"]["agent"]

    async def revoke_mid_read(*args, **kwargs):
        await runtime.receiver.revoke(
            Binding.model_validate({**binding, "idempotency_key": "revoke-mid", "fence": 2})
        )
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(ProtectedResolver, "resolve_current", revoke_mid_read)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE") as caught:
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))
    assert not isinstance(caught.value, MaterialLapsedError)


async def test_sweep_stops_expired_superseded_retained_without_grace(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable", status="monitoring_pr")
    await _install_replacement_grants(runtime, value)
    _advance_past_expiry(runtime, value, monkeypatch)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.authority_grace_seconds = 600
    scope.stop_workspace = AsyncMock()
    with structlog.testing.capture_logs() as logs:
        assert await scope.admission_check()
    scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert not path.exists()
    assert not any(log["event"] == "hosted_node.authority_grace_started" for log in logs)


@pytest.mark.parametrize("failure", ["revoked", "missing_receipt", "identity_mismatch"])
async def test_non_lapse_denials_keep_present_reasons(authority, failure):
    runtime, value, factory = authority
    binding = value["hosted"]["credentials"]["agent"]
    if failure == "revoked":
        await runtime.receiver.revoke(
            Binding.model_validate({**binding, "idempotency_key": "revoke", "fence": 2})
        )
        expected = "PROTECTED_MATERIAL_UNAVAILABLE"
    elif failure == "missing_receipt":
        async with factory.begin() as session:
            receipt = await runtime.receiver.receipt(session, Binding.model_validate(binding))
            assert receipt is not None
            await session.delete(receipt)
        expected = "PROTECTED_MATERIAL_UNAVAILABLE"
    else:
        for item in value["hosted"]["credentials"].values():
            item["identity"]["owner"] = "other/node"
        expected = "IDENTITY_MISMATCH"
    with pytest.raises(MaterialDeniedError, match=expected) as caught:
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))
    assert not isinstance(caught.value, MaterialLapsedError)


@pytest.mark.parametrize("status", ["monitoring_pr", "blocked"])
async def test_sweep_does_not_stop_lapsed_retained_workspace_inside_grace(
    authority, tmp_path, monkeypatch, status
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable", status=status)
    other = await retained(runtime, value, factory, "ws_other", other=True)
    expiry = _advance_past_expiry(runtime, value, monkeypatch)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.authority_grace_seconds = 600
    scope.stop_workspace = AsyncMock()
    with structlog.testing.capture_logs() as logs:
        assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert path.is_dir() and other.is_dir()
    started = [log for log in logs if log["event"] == "hosted_node.authority_grace_started"]
    assert len(started) == 1
    assert started[0]["workspace_id"] == "ws_cloud_stable"
    assert started[0]["reason_code"] == "PROTECTED_MATERIAL_LAPSED"
    assert started[0]["expires_at"] == expiry.isoformat()


async def test_sweep_clears_grace_after_successful_recheck(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(side_effect=[lapse, True])
    with structlog.testing.capture_logs() as logs:
        assert await scope.admission_check()
        assert "ws_exact" in scope._authority_grace
        assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace
    assert any(log["event"] == "hosted_node.authority_grace_cleared" for log in logs)


async def test_retained_reconcile_releases_leftover_workspace_claim(tmp_path, monkeypatch):
    """Reconcile success clears only reconcile; leftover watcher stays independent.

    Independent checker windows mean a deferred watcher leftover cannot poison
    a later reconcile lapse — reconcile opens its own fresh window
    (PRRT_kwDOSJAM6s64aTnL behaviour under per-checker clocks).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    # Scope-exit leftover on the watcher checker (finalizer deferred).
    assert scope._classify_authority_failure("ws_exact", lapse, checker="watcher") == "defer"
    assert set(scope._authority_grace["ws_exact"]) == {"watcher"}

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    authority.publication = AsyncMock(return_value=object())
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    # Reconcile clears only itself; watcher leftover remains (no cross-clear).
    assert set(scope._authority_grace["ws_exact"]) == {"watcher"}

    # A brief new reconcile lapse must start a fresh reconcile window.
    clock["now"] = origin + timedelta(seconds=601)
    fresh_lapse = MaterialLapsedError(expires_at=clock["now"] - timedelta(seconds=1))
    authority.reconcile_workspace = AsyncMock(side_effect=fresh_lapse)
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert set(scope._authority_grace["ws_exact"]) == {"watcher", "reconcile"}
    assert scope._authority_grace["ws_exact"]["reconcile"].started_at == fresh_lapse.expires_at


async def test_retained_reconcile_without_publication_preserves_workspace_claim(
    tmp_path, monkeypatch
):
    """Reconcile success must not reset a concurrent scope_entry grace window.

    Independent checkers: a successful retained sweep clears only reconcile,
    so a first-publish OSError on scope_entry keeps its HOSTED_AUTHORITY_UNAVAILABLE
    clock (PRRT_kwDOSJAM6s64aoW9).
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
        scope._classify_authority_failure(
            "ws_exact", OSError("first publish"), checker="scope_entry"
        )
        == "defer"
    )
    assert set(scope._authority_grace["ws_exact"]) == {"scope_entry"}
    started_at = scope._authority_grace["ws_exact"]["scope_entry"].started_at

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    authority.publication = AsyncMock(return_value=None)
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]["scope_entry"]
    assert window.started_at == started_at

    clock["now"] = origin + timedelta(seconds=601)
    assert (
        scope._classify_authority_failure(
            "ws_exact", OSError("first publish"), checker="scope_entry"
        )
        == "stop_exhausted"
    )


async def test_retained_reconcile_preserves_unrelated_resource_limits_claim(tmp_path, monkeypatch):
    """Successful reconcile must not clear an open resource_limits grace window."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    assert scope._classify_authority_failure("ws_exact", lapse, checker="watcher") == "defer"
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="resource_limits")
        == "defer"
    )
    assert set(scope._authority_grace["ws_exact"]) == {"watcher", "resource_limits"}
    started_at = scope._authority_grace["ws_exact"]["resource_limits"].started_at

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    authority.publication = AsyncMock(return_value=object())
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert set(scope._authority_grace["ws_exact"]) == {"watcher", "resource_limits"}
    assert scope._authority_grace["ws_exact"]["resource_limits"].started_at == started_at


async def test_workspace_success_releases_leftover_reconcile_claim(tmp_path, monkeypatch):
    """Watcher/scope success does not share a deadline with reconcile leftovers.

    A retained sweep timeout opens reconcile's window. Successful active-scope
    checks clear only their own checkers; a later brief watcher timeout still
    opens a fresh watcher window instead of exhausting against reconcile's
    started_at (PRRT_kwDOSJAM6s64an_5 under independent clocks).
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
    assert set(scope._authority_grace["ws_exact"]) == {"reconcile"}
    reconcile_started = scope._authority_grace["ws_exact"]["reconcile"].started_at

    async with scope.scope("ws_exact"):
        # Scope-bound checkers are cleared on healthy exit; reconcile remains.
        assert set(scope._authority_grace["ws_exact"]) == {"reconcile"}
    assert set(scope._authority_grace["ws_exact"]) == {"reconcile"}
    assert scope._authority_grace["ws_exact"]["reconcile"].started_at == reconcile_started
    scope.stop_workspace.assert_not_awaited()

    clock["now"] = origin + timedelta(seconds=602)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), checker="watcher") == "defer"
    )
    window = scope._authority_grace["ws_exact"]["watcher"]
    assert (clock["now"] - window.started_at).total_seconds() < 1


async def test_workspace_success_preserves_unrelated_resource_limits_claim(tmp_path, monkeypatch):
    """Successful scope recovery must not clear an open resource_limits window."""
    scope, _authority = _mock_consumers(tmp_path, grace=600)
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
    started_at = scope._authority_grace["ws_exact"]["resource_limits"].started_at

    async with scope.scope("ws_exact"):
        # While the scope is open, resource_limits remains; reconcile is independent.
        assert "resource_limits" in scope._authority_grace["ws_exact"]
        assert scope._authority_grace["ws_exact"]["resource_limits"].started_at == started_at


async def test_sweep_exhausts_grace_and_stops_with_authority_unavailable(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable", status="monitoring_pr")
    expiry = Binding.model_validate(value["hosted"]["credentials"]["agent"]).expires_at
    now = expiry + timedelta(seconds=600)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(now.timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.receiver.datetime", Clock)
    monkeypatch.setattr("awf.hosted_node.runtime_authority.datetime", Clock)
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.authority_grace_seconds = 600
    scope.stop_workspace = AsyncMock()
    with structlog.testing.capture_logs() as logs:
        assert await scope.admission_check()
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.args == ("ws_cloud_stable",)
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"
    assert "600" in scope.stop_workspace.await_args.kwargs["message"]
    assert not path.exists()
    assert any(log["event"] == "hosted_node.authority_grace_exhausted" for log in logs)


async def test_failed_stop_after_grace_exhaustion_does_not_restart_grace(tmp_path, monkeypatch):
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
    authority.reconcile_workspace = AsyncMock(side_effect=TimeoutError())
    authority.retract = AsyncMock(side_effect=[OperationalError("db", {}, None), None])

    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" in scope._authority_grace

    clock["now"] = origin + timedelta(seconds=1)
    with structlog.testing.capture_logs() as logs:
        assert not await scope.admission_check()
    assert "ws_exact" in scope._retained_stop_failures
    assert "ws_exact" in scope._authority_grace
    assert any(log["event"] == "hosted_node.authority_grace_exhausted" for log in logs)
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"

    with structlog.testing.capture_logs() as retry_logs:
        assert await scope.admission_check()
    assert "ws_exact" not in scope._retained_stop_failures
    assert "ws_exact" not in scope._authority_grace
    assert not any(log["event"] == "hosted_node.authority_grace_started" for log in retry_logs)
    assert any(log["event"] == "hosted_node.authority_grace_exhausted" for log in retry_logs)
    assert scope.stop_workspace.await_count == 2
    assert all(
        call.kwargs.get("reason_code") == "HOSTED_AUTHORITY_UNAVAILABLE"
        for call in scope.stop_workspace.await_args_list
    )


async def test_reconcile_defer_does_not_close_admission_for_prior_stop_failure(
    tmp_path, monkeypatch
):
    """Defer must not leave a prior stop failure blocking node admission.

    Reconcile ``continue`` on defer used to skip the outer discard/add paths for
    ``_retained_stop_failures``. A workspace already listed from a failed ``_stop``
    that later hits a lapse/transient stayed listed, so ``admission_check`` returned
    false and ``run_once`` skipped dispatch/recovery/GC for the grace window.
    Membership must remain for orphaned retry while admission stays open.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(side_effect=TimeoutError())
    scope._retained_stop_failures.add("ws_exact")

    assert await scope.admission_check()
    assert "ws_exact" in scope._retained_stop_failures
    assert "reconcile" in scope._authority_grace.get("ws_exact", {})
    scope.stop_workspace.assert_not_awaited()


async def test_timeout_then_stale_lapse_backdates_grace_and_exhausts(tmp_path, monkeypatch):
    """A transient-opened window must adopt an earlier material expiry on lapse."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    expiry = origin - timedelta(seconds=601)
    authority.reconcile_workspace = AsyncMock(
        side_effect=[TimeoutError(), MaterialLapsedError(expires_at=expiry)]
    )

    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]["reconcile"]
    assert window.started_at == origin
    assert window.expires_at is None

    with structlog.testing.capture_logs() as logs:
        assert await scope.admission_check()
    exhausted = [log for log in logs if log["event"] == "hosted_node.authority_grace_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["expires_at"] == expiry.isoformat()
    assert exhausted[0]["reason_code"] == "PROTECTED_MATERIAL_LAPSED"
    # Successful stop clears the exhausted window; exhaustion without advancing
    # the clock proves the transient-started window was backdated to expiry.
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"


async def test_watcher_lapse_inside_grace_keeps_activity(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        return material

    authority.workspace.side_effect = workspace
    async with scope.scope("ws_exact"):
        await asyncio.sleep(0.05)
        assert scope.workspace_id.get() == "ws_exact"
    scope.stop_workspace.assert_not_awaited()


async def test_watcher_revoke_inside_grace_stops_immediately(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
        return material

    authority.workspace.side_effect = workspace
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        async with scope.scope("ws_exact"):
            await asyncio.sleep(10)
    scope.stop_workspace.assert_awaited_once_with("ws_exact")


async def test_watcher_grace_exhaustion_stops_with_authority_unavailable(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=1)
    material = authority.workspace.return_value
    expiry = datetime.now(UTC) - timedelta(seconds=2)
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise MaterialLapsedError(expires_at=expiry)
        return material

    authority.workspace.side_effect = workspace
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            await asyncio.sleep(10)
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"


async def test_scope_entry_waits_out_lapse_then_enters_on_renewal(tmp_path):
    """Scope entry waits inside grace until material is current; no caller retry."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    renewed = AuthorizedMaterials(
        {"GH_TOKEN": "renewed", "GITHUB_TOKEN": "renewed", "CODEX_API_KEY": "renewed-agent"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lapse
        return renewed if calls["n"] > 2 else material

    authority.workspace.side_effect = workspace
    async with scope.scope("ws_exact"):
        assert scope.workspace_id.get() == "ws_exact"
        assert calls["n"] >= 2
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace


async def test_scope_entry_publication_denial_stops_and_raises_scope_denied(tmp_path):
    """Publication (association+materials) denial must stop and wrap like base."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    authority.publication.side_effect = MaterialDeniedError("GRANT_REVOKED", 403)
    with pytest.raises(WorkspaceScopeDeniedError, match="GRANT_REVOKED"):
        async with scope.scope("ws_exact"):
            pytest.fail("explicit publication denial must not enter")
    scope.stop_workspace.assert_awaited_once_with("ws_exact")
    authority.workspace.assert_not_awaited()


async def test_scope_entry_publication_lapse_waits_then_succeeds(tmp_path):
    """Publication lapse must wait under grace, start a new snapshot, then enter."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def publication(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lapse
        return object()

    authority.publication.side_effect = publication
    async with scope.scope("ws_exact"):
        assert scope.workspace_id.get() == "ws_exact"
        assert calls["n"] >= 2
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace


async def test_nested_same_workspace_scope_waits_out_lapse(tmp_path):
    """Nested same-workspace re-entry must use grace, not raw authority.workspace().

    Hosted monitor-resume enters an outer consumer scope then nests into
    Provisioner.ensure_hosted_monitor_worktree; a lapse at that nested check
    must wait inside grace instead of failing the checkout handler.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 2:
            raise lapse
        return material

    authority.workspace.side_effect = workspace
    async with scope.scope("ws_exact"):
        async with scope.scope("ws_exact"):
            assert scope.workspace_id.get() == "ws_exact"
        assert calls["n"] >= 3
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace


async def test_nested_same_workspace_scope_opt_out_raises_deferred(tmp_path, monkeypatch):
    """Nested re-entry honors without_authority_grace_wait like outer entry."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    material = authority.workspace.return_value
    calls = {"n": 0}
    slept: list[float] = []

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        return material

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    async with scope.scope("ws_exact"):
        with (
            without_authority_grace_wait(),
            pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"),
        ):
            async with scope.scope("ws_exact"):
                pytest.fail("nested opt-out must not wait or enter")
    assert slept == []
    scope.stop_workspace.assert_not_awaited()


async def test_nested_same_workspace_scope_grace_zero_raises_without_waiting(tmp_path, monkeypatch):
    """Nested re-entry with grace 0 must fail the lapse immediately (no sleep)."""
    scope, authority = _mock_consumers(tmp_path, grace=0)
    material = authority.workspace.return_value
    calls = {"n": 0}
    slept: list[float] = []

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        # Only the nested re-check lapses; outer entry/finalize stay healthy.
        if calls["n"] == 2:
            raise MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        return material

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    async with scope.scope("ws_exact"):
        with pytest.raises(MaterialLapsedError, match="PROTECTED_MATERIAL_LAPSED"):
            async with scope.scope("ws_exact"):
                pytest.fail("grace 0 nested lapse must not enter")
    assert slept == []
    assert calls["n"] >= 2


async def test_scope_entry_publication_exhausted_stops_unavailable(tmp_path, monkeypatch):
    """Repeated publication lapses past grace must stop with HOSTED_AUTHORITY_UNAVAILABLE."""
    scope, authority = _mock_consumers(tmp_path, grace=1)
    authority.publication.side_effect = MaterialLapsedError(
        expires_at=datetime.now(UTC) - timedelta(seconds=2)
    )
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)

    async def advance_on_sleep(seconds: float) -> None:
        clock["now"] = origin + timedelta(seconds=1)

    monkeypatch.setattr(
        "awf.hosted_node.runtime_consumers.asyncio.sleep",
        advance_on_sleep,
    )
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            pytest.fail("exhausted publication lapse must not enter")
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"


async def test_scope_entry_publication_grace_zero_stops_on_lapse(tmp_path):
    """Grace 0 via publication must match base: immediate stop, no wait."""
    scope, authority = _mock_consumers(tmp_path, grace=0)
    authority.publication.side_effect = MaterialLapsedError(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            pytest.fail("grace 0 publication lapse must stop")
    scope.stop_workspace.assert_awaited_once_with("ws_exact")


async def test_scope_entry_opt_out_raises_deferred_without_waiting(tmp_path, monkeypatch):
    """Poll-inline recovery opts out of the wait via ContextVar."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    authority.workspace.side_effect = MaterialLapsedError(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    with (
        without_authority_grace_wait(),
        pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"),
    ):
        async with scope.scope("ws_exact"):
            pytest.fail("opt-out must not wait or enter")
    assert slept == []
    scope.stop_workspace.assert_not_awaited()


async def test_scope_entry_cancel_during_grace_wait_is_not_authority_lost(
    tmp_path, monkeypatch
) -> None:
    """Cancel while waiting out a lapse must not stop as authority-lost."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    authority.workspace.side_effect = MaterialLapsedError(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    authority.association = AsyncMock(return_value=(object(), "codex"))
    authority.materials = AsyncMock(
        side_effect=MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    async def operation() -> None:
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled grace wait must not enter")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)


async def test_scope_entry_cancel_during_grace_keeps_grace_clock(tmp_path, monkeypatch) -> None:
    """Retaining the warm stack on cancel must not reset the grace allowance.

    ``_stop(..., authority_lost=False)`` leaves the runtime running; clearing
    the grace window would let a later scope/sweep reopen a full 600s bound
    while the same transient continues.
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
    lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    authority.workspace.side_effect = lapse
    authority.association = AsyncMock(return_value=(object(), "codex"))
    authority.materials = AsyncMock(side_effect=lapse)
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    async def operation() -> None:
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled grace wait must not enter")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(waiting.wait(), 1)
    assert "scope_entry" in scope._authority_grace["ws_exact"]
    started_at = scope._authority_grace["ws_exact"]["scope_entry"].started_at
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)
    assert "scope_entry" in scope._authority_grace["ws_exact"]
    assert scope._authority_grace["ws_exact"]["scope_entry"].started_at == started_at

    # Same continuing transient on a later scope_entry check must reuse the
    # clock, not open another full allowance after cancel preserved the stack.
    # Reconcile is an independent checker and does not share this deadline.
    clock["now"] = origin + timedelta(seconds=300)
    with structlog.testing.capture_logs() as logs:
        assert (
            scope._classify_authority_failure("ws_exact", lapse, checker="scope_entry") == "defer"
        )
    assert scope._authority_grace["ws_exact"]["scope_entry"].started_at == started_at
    assert not any(log["event"] == "hosted_node.authority_grace_started" for log in logs)


@pytest.mark.parametrize(
    ("cleanup_error", "expected_kwargs"),
    [
        (MaterialDeniedError("GRANT_REVOKED", 403), {}),
        (MaterialDeniedError("WORKSPACE_BINDING_MISMATCH", 403), {}),
        (
            MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=700)),
            {
                "reason_code": "HOSTED_AUTHORITY_UNAVAILABLE",
                "message": ("Hosted authority could not be confirmed for 600 seconds."),
            },
        ),
    ],
    ids=["revoked", "binding_mismatch", "grace_exhausted"],
)
async def test_scope_entry_cancel_during_grace_reclassifies_cleanup_denial(
    tmp_path, monkeypatch, cleanup_error, expected_kwargs
) -> None:
    """Cancel during grace wait must not preserve when cleanup finds a hard denial."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    authority.workspace.side_effect = MaterialLapsedError(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    authority.association = AsyncMock(return_value=(object(), "codex"))
    authority.materials = AsyncMock(side_effect=cleanup_error)
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    async def operation() -> None:
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled grace wait must not enter")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(waiting.wait(), 1)
    assert "ws_exact" in scope._authority_grace
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    scope.stop_workspace.assert_awaited_once_with("ws_exact", **expected_kwargs)


async def test_scope_entry_cancel_after_failed_snapshot_still_rechecks_authority(
    tmp_path, monkeypatch
) -> None:
    """Failed publication snapshot must not skip the cleanup authority recheck.

    Cancel during the grace wait after a transient snapshot OSError joins that
    already-failed entry. Classifying only the old exception would preserve the
    warm stack (authority_lost=False) without association()/materials(), so a
    revocation that landed during the wait would go unchecked.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    authority.publication.side_effect = OSError("snapshot offline")
    authority.association = AsyncMock(return_value=(object(), "codex"))
    authority.materials = AsyncMock(side_effect=MaterialDeniedError("GRANT_REVOKED", 403))
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    async def operation() -> None:
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled grace wait must not enter")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    # Fresh authority check must run even though the joined snapshot failed
    # (isolated reproduction previously saw zero association checks).
    assert authority.association.await_count == 1
    assert authority.materials.await_count == 1
    scope.stop_workspace.assert_awaited_once_with("ws_exact")


async def test_scope_entry_cancel_after_failed_snapshot_preserves_when_recheck_ok(
    tmp_path, monkeypatch
) -> None:
    """Joining a failed snapshot separately must still allow preserve-on-ok.

    After a transient publication OSError, cancel during the grace wait must
    recheck association()/materials(). When that fresh check succeeds, the
    warm runtime stays (authority_lost=False) rather than treating the stale
    snapshot failure as a hard stop.
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    authority.publication.side_effect = OSError("snapshot offline")
    authority.association = AsyncMock(return_value=(object(), "codex"))
    authority.materials = AsyncMock()
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    async def operation() -> None:
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled grace wait must not enter")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    assert authority.association.await_count == 1
    assert authority.materials.await_count == 1
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)


async def test_scope_entry_cancel_cleanup_renewal_clears_stale_grace(tmp_path, monkeypatch) -> None:
    """Successful cancelled-start recheck must drop the old grace window.

    If renewal lands before cleanup's fresh material check, that check
    authorizes and ``_stop(authority_lost=False)`` preserves the warm stack.
    Retaining the pre-renewal ``scope_entry`` clock would let a later separate
    lapse (material expiry near the new now) immediately exhaust a 600s window
    started before the renewal. Clear the cohort on success so the new lapse
    opens a fresh allowance.
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
    # Grace opened one second before origin (stale window start).
    clock["now"] = origin - timedelta(seconds=1)
    early_lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    authority.workspace.side_effect = early_lapse
    authority.association = AsyncMock(return_value=(object(), "codex"))
    # Cleanup discovers renewal: fresh material check succeeds.
    authority.materials = AsyncMock()
    waiting = asyncio.Event()

    async def block_sleep(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", block_sleep)

    async def operation() -> None:
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled grace wait must not enter")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(waiting.wait(), 1)
    assert "scope_entry" in scope._authority_grace["ws_exact"]
    stale_started = scope._authority_grace["ws_exact"]["scope_entry"].started_at
    clock["now"] = origin + timedelta(seconds=1)
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)
    assert "ws_exact" not in scope._authority_grace

    # Separate lapse ~601s after original material expiry: must open a fresh
    # window, not reuse the pre-renewal clock that would already be exhausted.
    clock["now"] = origin + timedelta(seconds=602)
    new_lapse = MaterialLapsedError(expires_at=origin + timedelta(seconds=601))
    with structlog.testing.capture_logs() as logs:
        assert (
            scope._classify_authority_failure("ws_exact", new_lapse, checker="scope_entry")
            == "defer"
        )
    window = scope._authority_grace["ws_exact"]["scope_entry"]
    assert window.started_at == new_lapse.expires_at
    assert window.started_at != stale_started
    assert any(log["event"] == "hosted_node.authority_grace_started" for log in logs)
