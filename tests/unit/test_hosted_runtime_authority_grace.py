"""Lapse classification and bounded hosted authority grace (AWF-1258)."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock

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


async def test_environment_does_not_sleep_under_shared_mirror_lock(tmp_path, monkeypatch):
    """Grace waits must not run while GitManager holds the shared mirror lock.

    Under the lock, a grace-eligible failure raises WorkspaceAuthorityDeferredError
    (not the raw lapse) so callers unlock and retry outside it instead of hitting
    terminal provision/execute failure handlers.
    """
    from awf.node.git_manager import holding_shared_mirror_lock

    scope, authority = _mock_consumers(tmp_path, grace=600)
    scope.poll_seconds = 10.0
    material = authority.workspace.return_value
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    slept: list[float] = []

    async def workspace(_workspace_id: str):
        if holding_shared_mirror_lock.get():
            raise lapse
        return material

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    authority.workspace.side_effect = workspace
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.asyncio.sleep", record_sleep)
    async with scope.scope("ws_exact"):
        token = holding_shared_mirror_lock.set(True)
        try:
            with pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"):
                await scope.environment()
        finally:
            holding_shared_mirror_lock.reset(token)
    assert slept == []


async def test_under_lock_defer_unlocks_and_retries_outside_mirror_lock(tmp_path):
    """A lapse under the lock must release it and succeed on an outside retry."""
    from awf.node.git_manager import holding_mirror_lock, holding_shared_mirror_lock

    scope, authority = _mock_consumers(tmp_path, grace=600)
    entry = authority.workspace.return_value
    renewed = AuthorizedMaterials(
        {"GH_TOKEN": "renewed", "GITHUB_TOKEN": "renewed", "CODEX_API_KEY": "renewed-agent"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    lapse = MaterialLapsedError(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    calls = {"n": 0}
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()

    async def workspace(_workspace_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return entry
        if holding_shared_mirror_lock.get():
            raise lapse
        return renewed

    authority.workspace.side_effect = workspace
    async with scope.scope("ws_exact"):
        with pytest.raises(WorkspaceAuthorityDeferredError, match="PROTECTED_MATERIAL_LAPSED"):
            async with holding_mirror_lock(mirror, command_environment=scope.git_environment):
                await scope.git_environment()
        assert holding_shared_mirror_lock.get() is False
        env = await scope.git_environment()
    assert env["GH_TOKEN"] == "renewed"
    assert "ws_exact" not in scope._authority_grace


async def test_git_environment_waits_outside_shared_mirror_lock(tmp_path):
    """Outside the lock, git_environment still waits out a brief lapse."""
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
