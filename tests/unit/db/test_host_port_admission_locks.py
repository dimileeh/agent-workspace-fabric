"""Focused tests for host-port and owned-path advisory lock helpers."""

from __future__ import annotations

from typing import Any

import pytest

from awf.db.repositories import workspace_repo_host_ports


class _RecordingSession:
    def __init__(self) -> None:
        self.executed: list[tuple[Any, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> object:
        self.executed.append((statement, params))
        return object()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owned_path_conflict_lock_noops_for_non_postgres() -> None:
    """Non-PostgreSQL dialects cannot use advisory locks and must be skipped."""
    session = _RecordingSession()

    await workspace_repo_host_ports.acquire_owned_path_conflict_lock(
        session,  # type: ignore[arg-type]
        "sqlite",
        repo_url="git@github.com:example/repo.git",
        branch_base="main",
        owned_paths=["src/awf/**"],
    )

    assert session.executed == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_host_port_admission_lock_noops_without_postgres_lock_scope() -> None:
    """Empty scans and non-PostgreSQL dialects do not execute lock SQL."""
    empty_session = _RecordingSession()
    await workspace_repo_host_ports.acquire_host_port_admission_lock(
        empty_session,  # type: ignore[arg-type]
        "postgresql",
        host_ports=[],
    )

    sqlite_session = _RecordingSession()
    await workspace_repo_host_ports.acquire_host_port_admission_lock(
        sqlite_session,  # type: ignore[arg-type]
        "sqlite",
        host_ports=[8080],
    )

    assert empty_session.executed == []
    assert sqlite_session.executed == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_host_port_admission_lock_skips_duplicate_lock_key_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hash collision must not acquire the same advisory lock twice."""
    session = _RecordingSession()
    monkeypatch.setattr(
        workspace_repo_host_ports,
        "_host_port_admission_advisory_lock_key",
        lambda _port: 42,
    )

    await workspace_repo_host_ports.acquire_host_port_admission_lock(
        session,  # type: ignore[arg-type]
        "postgresql",
        host_ports=[3001, 3002, 3001],
    )

    assert len(session.executed) == 1
    assert session.executed[0][1] == {"lock_key": 42}
