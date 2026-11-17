"""Lapse classification and bounded hosted authority grace (AWF-1258)."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.commands import FakeCommandRunner
from awf.common.workspace_scope import without_authority_grace_wait
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import (
    MaterialDeniedError,
    MaterialLapsedError,
    WorkspaceAuthorityDeferredError,
    WorkspaceScopeDeniedError,
)
from awf.hosted_node.models import Receipt
from awf.hosted_node.runtime_authority import AuthorizedMaterials
from awf.hosted_node.runtime_consumers import HostedCommandRunner, WorkspaceConsumers
from awf.hosted_node.schemas import Binding, Install
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_review_retained import retained


async def _install_replacement_grants(runtime, value) -> None:
    """Install higher-fence grants so the association's grants are preparation-superseded."""
    for role, binding in value["hosted"]["credentials"].items():
        replacement = {
            **binding,
            "grant_id": f"{role}-replacement",
            "idempotency_key": f"{role}-replacement",
            "fence": binding["fence"] + 1,
            "material": base64.b64encode(f"replacement-{role}".encode()).decode(),
        }
        result = await runtime.receiver.install(Install.model_validate(replacement))
        assert result.status == "current"


async def _advance_same_grant_current(runtime, factory, selected) -> None:
    """Point grant.current at a newer same-grant receipt (post-selection renewal race)."""
    async with factory.begin() as session:
        for name, prior in selected.items():
            prep = await runtime.receiver.preparation(session, prior)
            assert prep is not None
            grant = await runtime.receiver.grant(session, prep, prior)
            old = await runtime.receiver.receipt(session, prior)
            assert grant is not None and old is not None and grant.current == old.id
            slot = uuid4().hex
            renewed = {
                **runtime.receiver.binding(prior),
                "revision": prior.revision + 1,
                "fence": prior.fence + 1,
                "idempotency_key": f"{name}-post-expiry-renewal",
            }
            session.add(
                Receipt(
                    id=slot,
                    owner=runtime.receiver.identity.owner,
                    idempotency_key=renewed["idempotency_key"],
                    grant=grant.id,
                    binding=renewed,
                    operation="refresh",
                    predecessor=old.id,
                    state="installed",
                )
            )
            grant.current = slot
            grant.revision = prior.revision + 1
            grant.fence = prior.fence + 1


def _advance_past_expiry(runtime, value, monkeypatch, *, extra_seconds: float = 1) -> datetime:
    expiry = Binding.model_validate(value["hosted"]["credentials"]["agent"]).expires_at
    now = expiry + timedelta(seconds=extra_seconds)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(now.timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.receiver.datetime", Clock)
    monkeypatch.setattr("awf.hosted_node.runtime_authority.datetime", Clock)
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    return expiry


def _mock_consumers(tmp_path, *, grace: int = 600) -> tuple[WorkspaceConsumers, AsyncMock]:
    authority = AsyncMock()
    authority.receiver.config.hosted_authority_grace_seconds = grace
    material = AuthorizedMaterials(
        {"GH_TOKEN": "g", "GITHUB_TOKEN": "g", "CODEX_API_KEY": "a"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    authority.workspace.return_value = material
    scope = WorkspaceConsumers(authority, tmp_path)
    scope.authority_grace_seconds = grace
    scope.poll_seconds = 0.01
    scope.stop_workspace = AsyncMock()
    return scope, authority


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
    """Healthy retained reconcile must drop a deferred workspace grace claim.

    An active-scope finalizer that defers on a transient leaves
    ``failing_operations={"workspace"}``. Successful retained sweeps used to
    clear only ``reconcile``, so the original deadline survived; a later
    one-second lapse after that deadline exhausted immediately instead of
    opening a fresh grace window (PRRT_kwDOSJAM6s64aTnL).

    Clearing the workspace claim requires a live published view: reconcile
    with ``publish_view=False`` does not prove first-publish recovery.
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
    # Scope-exit leftover: workspace claim still open under the shared deadline.
    assert scope._classify_authority_failure("ws_exact", lapse, operation="workspace") == "defer"
    assert scope._authority_grace["ws_exact"].failing_operations == {"workspace"}

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    # Live publication means reconcile exercised (or could exercise) publish.
    authority.publication = AsyncMock(return_value=object())
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert "ws_exact" not in scope._authority_grace

    # After a healthy reconcile cleared the leftover claim, a brief new lapse
    # must start a fresh grace window — not reuse the exhausted deadline.
    clock["now"] = origin + timedelta(seconds=601)
    fresh_lapse = MaterialLapsedError(expires_at=clock["now"] - timedelta(seconds=1))
    authority.reconcile_workspace = AsyncMock(side_effect=fresh_lapse)
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]
    assert window.failing_operations == {"reconcile"}
    assert window.started_at == fresh_lapse.expires_at


async def test_retained_reconcile_without_publication_preserves_workspace_claim(
    tmp_path, monkeypatch
):
    """Non-publishing reconcile must not reset a concurrent workspace grace claim.

    ``reconcile_workspace`` uses ``publish_view=False`` when no live token or
    retry marker exists, while in-scope ``workspace()`` always publishes. A
    successful authority-only sweep must not clear a first-publish OSError
    grace window and restart the HOSTED_AUTHORITY_UNAVAILABLE clock
    (PRRT_kwDOSJAM6s64aoW9).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    # Concurrent scope-entry first-publish failure: workspace claim open, no view.
    assert (
        scope._classify_authority_failure(
            "ws_exact", OSError("first publish"), operation="workspace"
        )
        == "defer"
    )
    assert scope._authority_grace["ws_exact"].failing_operations == {"workspace"}
    started_at = scope._authority_grace["ws_exact"].started_at

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    authority.publication = AsyncMock(return_value=None)
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]
    assert window.failing_operations == {"workspace"}
    assert window.started_at == started_at

    # Later first-publish failures must still accumulate against the same window.
    clock["now"] = origin + timedelta(seconds=601)
    assert (
        scope._classify_authority_failure(
            "ws_exact", OSError("first publish"), operation="workspace"
        )
        == "stop_exhausted"
    )


async def test_retained_reconcile_preserves_unrelated_resource_limits_claim(tmp_path, monkeypatch):
    """Successful reconcile must not clear an open resource_limits grace claim."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    assert scope._classify_authority_failure("ws_exact", lapse, operation="workspace") == "defer"
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), operation="resource_limits")
        == "defer"
    )
    assert scope._authority_grace["ws_exact"].failing_operations == {
        "workspace",
        "resource_limits",
    }
    started_at = scope._authority_grace["ws_exact"].started_at

    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    authority.publication = AsyncMock(return_value=object())
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]
    assert window.failing_operations == {"resource_limits"}
    assert window.started_at == started_at


async def test_workspace_success_releases_leftover_reconcile_claim(tmp_path, monkeypatch):
    """Healthy active-scope workspace checks must drop a deferred reconcile claim.

    A retained sweep timeout leaves ``failing_operations={"reconcile"}``.
    Successful workspace checks used to clear only ``workspace``, so the
    original deadline survived; a later brief workspace timeout after that
    deadline exhausted immediately instead of opening a fresh grace window
    (PRRT_kwDOSJAM6s64an_5).
    """
    scope, authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    # Retained-sweep leftover: reconcile claim still open under the shared deadline.
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), operation="reconcile")
        == "defer"
    )
    assert scope._authority_grace["ws_exact"].failing_operations == {"reconcile"}

    # Successful scope entry/finalizer must release the equivalent reconcile claim.
    async with scope.scope("ws_exact"):
        assert "ws_exact" not in scope._authority_grace
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()

    # After recovery cleared the leftover claim, a brief new timeout must start
    # a fresh grace window — not reuse the exhausted deadline.
    clock["now"] = origin + timedelta(seconds=602)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), operation="workspace")
        == "defer"
    )
    window = scope._authority_grace["ws_exact"]
    assert window.failing_operations == {"workspace"}
    assert (clock["now"] - window.started_at).total_seconds() < 1


async def test_workspace_success_preserves_unrelated_resource_limits_claim(tmp_path, monkeypatch):
    """Successful workspace recovery must not clear an open resource_limits claim."""
    scope, _authority = _mock_consumers(tmp_path, grace=600)
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), operation="reconcile")
        == "defer"
    )
    assert (
        scope._classify_authority_failure("ws_exact", TimeoutError(), operation="resource_limits")
        == "defer"
    )
    started_at = scope._authority_grace["ws_exact"].started_at

    async with scope.scope("ws_exact"):
        window = scope._authority_grace["ws_exact"]
        assert window.failing_operations == {"resource_limits"}
        assert window.started_at == started_at


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
    window = scope._authority_grace["ws_exact"]
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
    assert "ws_exact" in scope._authority_grace
    started_at = scope._authority_grace["ws_exact"].started_at
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)
    assert "ws_exact" in scope._authority_grace
    assert scope._authority_grace["ws_exact"].started_at == started_at

    # Same continuing transient on a retained sweep must reuse the clock, not
    # open another full allowance after cancel cleanup preserved the stack.
    clock["now"] = origin + timedelta(seconds=300)
    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(side_effect=lapse)
    with structlog.testing.capture_logs() as logs:
        assert await scope.admission_check()
    scope.stop_workspace.assert_awaited_once()
    assert "ws_exact" in scope._authority_grace
    assert scope._authority_grace["ws_exact"].started_at == started_at
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
        # Same clear the watcher performs after a successful workspace() poll
        # (workspace recovery also releases an equivalent reconcile claim).
        scope._clear_authority_grace("ws_exact", operation="workspace")
        scope._clear_authority_grace("ws_exact", operation="reconcile")
        raise TimeoutError()

    authority.resource_limits = AsyncMock(side_effect=resource_limits)

    with pytest.raises(TimeoutError):
        await scope.resource_limits("ws_exact")

    assert limits_calls["n"] >= 3
    scope.stop_workspace.assert_awaited_once()
    assert scope.stop_workspace.await_args.kwargs["reason_code"] == "HOSTED_AUTHORITY_UNAVAILABLE"


async def test_stack_resource_limits_cancel_releases_claim_so_later_lapse_defers(
    tmp_path, monkeypatch
):
    """Cancel during resource_limits grace wait must not leave a leaked claim.

    ``_await_with_authority_grace`` used to keep the operation claim after
    CancelledError. Healthy reconcile clears only reconcile/workspace, so a
    leaked resource_limits claim retained the old started_at; a brief lapse
    601s later exhausted immediately and stopped the workspace.
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
    assert scope._authority_grace["ws_exact"].failing_operations == {"resource_limits"}
    task.cancel("operator cancel during grace wait")
    with pytest.raises(asyncio.CancelledError, match="operator cancel during grace wait"):
        await task
    assert "ws_exact" not in scope._authority_grace
    scope.stop_workspace.assert_not_awaited()

    # Healthy reconcile must not find a leftover reservation claim.
    authority.admission = AsyncMock()
    authority.retained_workspaces = AsyncMock(return_value=["ws_exact"])
    authority.reconcile_workspace = AsyncMock(return_value=True)
    assert await scope.admission_check()
    assert "ws_exact" not in scope._authority_grace

    # A brief new lapse long after the cancelled wait must open a fresh window.
    clock["now"] = origin + timedelta(seconds=601)
    fresh = MaterialLapsedError(expires_at=clock["now"] - timedelta(seconds=1))
    authority.reconcile_workspace = AsyncMock(side_effect=fresh)
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    window = scope._authority_grace["ws_exact"]
    assert window.failing_operations == {"reconcile"}
    assert window.started_at == fresh.expires_at


async def test_stack_resource_limits_opt_out_releases_claim(tmp_path, monkeypatch):
    """Opt-out DeferredError must release only the resource_limits grace claim."""
    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    origin = datetime.now(UTC)
    clock = {"now": origin}

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock["now"].timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    # Unrelated workspace claim must survive releasing the reservation claim.
    lapse = MaterialLapsedError(expires_at=origin - timedelta(seconds=1))
    assert scope._classify_authority_failure("ws_exact", lapse, operation="workspace") == "defer"
    started_at = scope._authority_grace["ws_exact"].started_at

    async def record_sleep(seconds: float) -> None:
        raise AssertionError("resource_limits must not sleep under grace opt-out")

    authority.resource_limits = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    with (
        without_authority_grace_wait(),
        pytest.raises(WorkspaceAuthorityDeferredError),
    ):
        await scope.resource_limits("ws_exact")
    window = scope._authority_grace["ws_exact"]
    assert window.failing_operations == {"workspace"}
    assert window.started_at == started_at
    scope.stop_workspace.assert_not_awaited()

    # After the workspace claim is also cleared, a late lapse starts fresh.
    scope._clear_authority_grace("ws_exact", operation="workspace")
    assert "ws_exact" not in scope._authority_grace
    clock["now"] = origin + timedelta(seconds=601)
    fresh = MaterialLapsedError(expires_at=clock["now"] - timedelta(seconds=1))
    assert scope._classify_authority_failure("ws_exact", fresh, operation="reconcile") == "defer"
    assert scope._authority_grace["ws_exact"].started_at == fresh.expires_at


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
