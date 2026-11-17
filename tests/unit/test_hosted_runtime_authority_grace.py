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
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import (
    MaterialDeniedError,
    MaterialLapsedError,
    WorkspaceScopeDeniedError,
)
from awf.hosted_node.runtime_authority import AuthorizedMaterials
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.schemas import Binding, Install
from tests.unit.test_hosted_runtime_authority import authority as authority
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


async def test_scope_entry_lapse_denies_without_stop_then_renewal_succeeds(tmp_path):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    authority.workspace.side_effect = MaterialLapsedError(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_LAPSED"):
        async with scope.scope("ws_exact"):
            pytest.fail("lapsed scope must not start")
    scope.stop_workspace.assert_not_awaited()
    authority.workspace.side_effect = None
    authority.workspace.return_value = material
    async with scope.scope("ws_exact"):
        assert scope.workspace_id.get() == "ws_exact"
    assert "ws_exact" not in scope._authority_grace


@pytest.mark.parametrize(
    "error", [TimeoutError(), OSError("offline"), OperationalError("db", {}, None)]
)
@pytest.mark.parametrize("place", ["scope", "watcher", "sweep"])
async def test_single_transient_then_success_does_not_stop(tmp_path, error, place):
    scope, authority = _mock_consumers(tmp_path, grace=600)
    material = authority.workspace.return_value
    if place == "scope":
        authority.workspace.side_effect = [error, material, material]
        with pytest.raises(type(error)):
            async with scope.scope("ws_exact"):
                pytest.fail("transient scope entry must defer")
        scope.stop_workspace.assert_not_awaited()
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
        authority.workspace.side_effect = error
        with pytest.raises(type(error)):
            async with scope.scope("ws_exact"):
                pytest.fail("first transient defers")
        scope.stop_workspace.assert_not_awaited()
        clock["now"] = origin + timedelta(seconds=1)
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
