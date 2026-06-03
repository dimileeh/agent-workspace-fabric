"""Tests for the shared ``create_workspace_row_checked`` workspace-create helper.

The helper is the de-duplicated core of the REST route handler and
``WorkspaceService.create``: it resolves the profile snapshot once, guards
host-port conflicts, and persists the row, forwarding the snapshot to both
collaborators so the profile is not resolved twice (issue #347).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import awf.service.workspaces_create as workspaces_create
from awf.service.workspaces import (
    WorkspaceCreateDuplicateHostPortError,
    WorkspaceCreateHostPortConflictError,
    create_workspace_row_checked,
)


def _install_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    port_check: AsyncMock,
    create_row: AsyncMock,
    snapshot: tuple[Any, Any],
) -> dict[str, Any]:
    """Patch the helper's three collaborators and return a snapshot call counter."""
    calls: dict[str, Any] = {"snapshot": 0}

    def _snapshot_spy(payload: Any) -> tuple[Any, Any]:
        calls["snapshot"] += 1
        return snapshot

    monkeypatch.setattr(workspaces_create, "workspace_create_profile_snapshots", _snapshot_spy)
    # ``check_host_port_conflicts`` is imported lazily from ``awf.service.workspaces``
    # inside the helper body, so patch it on that module.
    import awf.service.workspaces as workspaces_mod

    monkeypatch.setattr(workspaces_mod, "check_host_port_conflicts", port_check)
    monkeypatch.setattr(workspaces_create, "create_workspace_row", create_row)
    return calls


@pytest.mark.asyncio
async def test_resolves_snapshot_once_and_forwards_to_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile snapshot is computed once and forwarded to both collaborators."""
    req_profile = {"requested": True}
    resolved_profile = {"resolved": True}
    created = object()
    port_check = AsyncMock(return_value=None)
    create_row = AsyncMock(return_value=created)
    calls = _install_spies(
        monkeypatch,
        port_check=port_check,
        create_row=create_row,
        snapshot=(req_profile, resolved_profile),
    )

    session = object()
    repo = object()
    companions = object()
    payload = SimpleNamespace(companions=companions)
    result = await create_workspace_row_checked(
        session,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        payload,  # type: ignore[arg-type]
        idempotency_key="idem-1",
        settings=None,
        disk_check=None,
        node_id="node-a",
        excluding_workspace_id=None,
    )

    assert result is created
    assert calls["snapshot"] == 1
    port_check.assert_awaited_once_with(
        repo,
        companions,
        resolved_profile=resolved_profile,
        excluding_workspace_id=None,
        node_id="node-a",
    )
    create_row.assert_awaited_once_with(
        session,
        payload,
        idempotency_key="idem-1",
        settings=None,
        disk_check=None,
        requested_profile=req_profile,
        resolved_profile=resolved_profile,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        WorkspaceCreateHostPortConflictError(host_port=8080, conflicting_workspace_id="ws-other"),
        WorkspaceCreateDuplicateHostPortError(host_port=5432),
    ],
)
async def test_propagates_host_port_conflict(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A host-port conflict re-raises and ``create_workspace_row`` is never called."""
    port_check = AsyncMock(side_effect=error)
    create_row = AsyncMock()
    _install_spies(
        monkeypatch,
        port_check=port_check,
        create_row=create_row,
        snapshot=({"requested": True}, {"resolved": True}),
    )

    with pytest.raises(type(error)):
        await create_workspace_row_checked(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            SimpleNamespace(companions=object()),  # type: ignore[arg-type]
            node_id="node-a",
        )

    create_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwards_disk_check_settings_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``disk_check``, ``settings`` and ``idempotency_key`` reach ``create_workspace_row``."""
    sentinel_settings = object()
    sentinel_disk_check = object()
    create_row = AsyncMock(return_value=object())
    _install_spies(
        monkeypatch,
        port_check=AsyncMock(return_value=None),
        create_row=create_row,
        snapshot=({"requested": True}, {"resolved": True}),
    )

    await create_workspace_row_checked(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        SimpleNamespace(companions=object()),  # type: ignore[arg-type]
        idempotency_key="idem-xyz",
        settings=sentinel_settings,  # type: ignore[arg-type]
        disk_check=sentinel_disk_check,  # type: ignore[arg-type]
        node_id="node-b",
    )

    _, kwargs = create_row.await_args
    assert kwargs["idempotency_key"] == "idem-xyz"
    assert kwargs["settings"] is sentinel_settings
    assert kwargs["disk_check"] is sentinel_disk_check
