"""Shared mirror lock must not cover hosted authority grace waits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.node.git_manager import (
    GitManager,
    holding_mirror_lock,
    holding_shared_mirror_lock,
)


@pytest.mark.asyncio
async def test_holding_mirror_lock_resolves_command_env_before_lock(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    events: list[tuple[str, bool, bool]] = []

    async def command_environment() -> dict[str, str]:
        lock = GitManager._lock_for_mirror(mirror)
        events.append(
            (
                "command_environment",
                lock.locked(),
                holding_shared_mirror_lock.get(),
            )
        )
        await asyncio.sleep(0.02)
        return {"PATH": "/bin"}

    manager = GitManager(tmp_path / "work", command_environment=command_environment)
    async with manager._holding_mirror_lock(mirror):
        lock = GitManager._lock_for_mirror(mirror)
        events.append(("under_lock", lock.locked(), holding_shared_mirror_lock.get()))

    assert events[0] == ("command_environment", False, False)
    assert events[1] == ("under_lock", True, True)
    assert holding_shared_mirror_lock.get() is False


@pytest.mark.asyncio
async def test_holding_mirror_lock_does_not_block_peers_during_env_wait(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    env_waiting = asyncio.Event()
    release_env = asyncio.Event()
    order: list[str] = []

    async def slow_environment() -> dict[str, str]:
        order.append("env_wait_start")
        env_waiting.set()
        await release_env.wait()
        order.append("env_wait_done")
        return {}

    manager = GitManager(tmp_path / "work", command_environment=slow_environment)

    async def holder() -> None:
        async with manager._holding_mirror_lock(mirror):
            order.append("holder_under_lock")

    async def peer() -> None:
        await env_waiting.wait()
        async with holding_mirror_lock(mirror):
            order.append("peer_under_lock")
            release_env.set()

    await asyncio.wait_for(asyncio.gather(holder(), peer()), timeout=2.0)
    assert order == [
        "env_wait_start",
        "peer_under_lock",
        "env_wait_done",
        "holder_under_lock",
    ]
    assert holding_shared_mirror_lock.get() is False
