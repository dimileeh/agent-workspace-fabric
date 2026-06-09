"""Unit tests for merge coordinators.

The in-process coordinator serializes merge attempts inside one worker process
by ``repo_url + base_branch``. The Postgres coordinator uses the same keying
contract to serialize independently spawned service workers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.runtime import merge_coordinator as merge_coordinator_mod
from awf.runtime.merge_coordinator import InProcessMergeCoordinator

REPO_URL = "git@github.com:dimileeh/aira-web.git"


class TestInProcessMergeCoordinator:
    @pytest.mark.unit
    async def test_serializes_same_repo_and_base_branch(self) -> None:
        coordinator = InProcessMergeCoordinator()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                order.append("first-entered")
                first_entered.set()
                await release_first.wait()
                order.append("first-leaving")

        async def second() -> None:
            await first_entered.wait()
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                order.append("second-entered")

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_entered.wait()
        await asyncio.sleep(0)

        assert order == ["first-entered"]

        release_first.set()
        await asyncio.gather(first_task, second_task)

        assert order == ["first-entered", "first-leaving", "second-entered"]

    @pytest.mark.unit
    async def test_different_base_branches_do_not_block_each_other(self) -> None:
        coordinator = InProcessMergeCoordinator()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first() -> None:
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                first_entered.set()
                await release_first.wait()

        async def second() -> None:
            await first_entered.wait()
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="main",
            ):
                second_entered.set()

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_entered.wait()

        await asyncio.wait_for(second_entered.wait(), timeout=1)

        release_first.set()
        await asyncio.gather(first_task, second_task)

    @pytest.mark.unit
    async def test_prunes_lock_entry_when_merge_lane_is_idle(self) -> None:
        coordinator = InProcessMergeCoordinator()

        async with coordinator.serialized_merge(
            repo_url=REPO_URL,
            base_branch="development",
        ):
            pass

        loop_locks = coordinator._locks_by_loop[asyncio.get_running_loop()]

        assert loop_locks == {}

    @pytest.mark.unit
    async def test_release_entry_ignores_missing_loop_bucket(self) -> None:
        coordinator = InProcessMergeCoordinator()
        key = merge_coordinator_mod.MergeCoordinationKey.from_values(
            repo_url=REPO_URL,
            base_branch="development",
        )
        entry = merge_coordinator_mod._MergeLockEntry(lock=asyncio.Lock(), ref_count=1)

        coordinator._release_entry(key, entry)

        assert asyncio.get_running_loop() not in coordinator._locks_by_loop

    @pytest.mark.unit
    async def test_retains_lock_entry_while_task_waits(self) -> None:
        coordinator = InProcessMergeCoordinator()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                first_entered.set()
                await release_first.wait()

        async def second() -> None:
            await first_entered.wait()
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                pass

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_entered.wait()
        await asyncio.sleep(0)

        loop_locks = coordinator._locks_by_loop[asyncio.get_running_loop()]

        assert len(loop_locks) == 1

        release_first.set()
        await asyncio.gather(first_task, second_task)

        assert loop_locks == {}

    @pytest.mark.unit
    def test_reuses_coordinator_across_event_loops_after_contention(self) -> None:
        coordinator = InProcessMergeCoordinator()

        async def contend_once() -> None:
            first_entered = asyncio.Event()
            release_first = asyncio.Event()
            order: list[str] = []

            async def first() -> None:
                async with coordinator.serialized_merge(
                    repo_url=REPO_URL,
                    base_branch="development",
                ):
                    order.append("first-entered")
                    first_entered.set()
                    await release_first.wait()

            async def second() -> None:
                await first_entered.wait()
                async with coordinator.serialized_merge(
                    repo_url=REPO_URL,
                    base_branch="development",
                ):
                    order.append("second-entered")

            first_task = asyncio.create_task(first())
            second_task = asyncio.create_task(second())
            await first_entered.wait()
            await asyncio.sleep(0)

            assert order == ["first-entered"]

            release_first.set()
            await asyncio.gather(first_task, second_task)

            assert order == ["first-entered", "second-entered"]

        asyncio.run(contend_once())
        asyncio.run(contend_once())


class TestPostgresAdvisoryMergeCoordinator:
    @pytest.mark.unit
    def test_advisory_lock_key_is_deterministic_and_normalized(self) -> None:
        key = merge_coordinator_mod.postgres_advisory_lock_key(
            repo_url=REPO_URL,
            base_branch="development",
        )

        assert key == -4806809152263605362
        assert key == merge_coordinator_mod.postgres_advisory_lock_key(
            repo_url=f" {REPO_URL} ",
            base_branch=" development ",
        )
        assert key != merge_coordinator_mod.postgres_advisory_lock_key(
            repo_url=REPO_URL,
            base_branch="main",
        )
        assert -(2**63) <= key <= 2**63 - 1

    @pytest.mark.unit
    async def test_connect_asyncpg_delegates_to_asyncpg_connect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        connection = SimpleNamespace()

        async def _connect(*, dsn: str) -> object:
            assert dsn == "postgresql://awf@db/awf"
            return connection

        monkeypatch.setattr(merge_coordinator_mod.asyncpg, "connect", _connect)

        assert (
            await merge_coordinator_mod._connect_asyncpg(  # noqa: SLF001
                "postgresql://awf@db/awf"
            )
            is connection
        )

    @pytest.mark.unit
    async def test_context_manager_acquires_before_body_and_releases_after_body(self) -> None:
        calls: list[object] = []
        coordinator = merge_coordinator_mod.PostgresAdvisoryMergeCoordinator(
            _fake_engine(),
            connect=_FakeAdvisoryConnector(calls).connect,
        )

        async with coordinator.serialized_merge(
            repo_url=REPO_URL,
            base_branch="development",
        ):
            calls.append("body")

        expected_key = -4806809152263605362
        assert calls == [
            ("connect", "postgresql://awf:secret@db:5432/awf"),
            ("try-lock", expected_key),
            "body",
            ("unlock", expected_key),
            "close",
        ]

    @pytest.mark.unit
    async def test_context_manager_releases_lock_when_body_raises(self) -> None:
        calls: list[object] = []
        coordinator = merge_coordinator_mod.PostgresAdvisoryMergeCoordinator(
            _fake_engine(),
            connect=_FakeAdvisoryConnector(calls).connect,
        )

        with pytest.raises(RuntimeError, match="merge failed"):
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                calls.append("body")
                raise RuntimeError("merge failed")

        expected_key = -4806809152263605362
        assert calls == [
            ("connect", "postgresql://awf:secret@db:5432/awf"),
            ("try-lock", expected_key),
            "body",
            ("unlock", expected_key),
            "close",
        ]

    @pytest.mark.unit
    async def test_contenders_close_connections_between_try_lock_polls(self) -> None:
        calls: list[object] = []
        coordinator = merge_coordinator_mod.PostgresAdvisoryMergeCoordinator(
            _fake_engine(),
            poll_interval_seconds=0,
            connect=_FakeAdvisoryConnector(calls, try_lock_results=[False, True]).connect,
        )

        async with coordinator.serialized_merge(
            repo_url=REPO_URL,
            base_branch="development",
        ):
            calls.append("body")

        expected_key = -4806809152263605362
        assert calls == [
            ("connect", "postgresql://awf:secret@db:5432/awf"),
            ("try-lock", expected_key),
            "close",
            ("connect", "postgresql://awf:secret@db:5432/awf"),
            ("try-lock", expected_key),
            "body",
            ("unlock", expected_key),
            "close",
        ]

    @pytest.mark.unit
    async def test_try_lock_errors_close_connection_before_reraising(self) -> None:
        calls: list[object] = []
        coordinator = merge_coordinator_mod.PostgresAdvisoryMergeCoordinator(
            _fake_engine(),
            connect=_FailingAdvisoryConnector(calls).connect,
        )

        with pytest.raises(RuntimeError, match="try-lock failed"):
            async with coordinator.serialized_merge(
                repo_url=REPO_URL,
                base_branch="development",
            ):
                raise AssertionError("lock body should not run")

        assert calls == [
            ("connect", "postgresql://awf:secret@db:5432/awf"),
            ("try-lock", -4806809152263605362),
            "close",
        ]

    @pytest.mark.unit
    def test_asyncpg_dsn_from_engine_preserves_plain_driver_name(self) -> None:
        engine = cast(
            AsyncEngine,
            SimpleNamespace(url=make_url("postgresql://awf:secret@db:5432/awf")),
        )

        assert (
            merge_coordinator_mod._asyncpg_dsn_from_engine(engine)
            == "postgresql://awf:secret@db:5432/awf"
        )


def _fake_engine() -> AsyncEngine:
    return cast(
        AsyncEngine,
        SimpleNamespace(url=make_url("postgresql+asyncpg://awf:secret@db:5432/awf")),
    )


class _FakeAdvisoryConnector:
    def __init__(
        self,
        calls: list[object],
        *,
        try_lock_results: list[bool] | None = None,
    ) -> None:
        self._calls = calls
        self._try_lock_results = try_lock_results or [True]

    async def connect(self, dsn: str) -> _FakeAdvisoryConnection:
        self._calls.append(("connect", dsn))
        return _FakeAdvisoryConnection(self._calls, self._try_lock_results)


class _FakeAdvisoryConnection:
    def __init__(self, calls: list[object], try_lock_results: list[bool]) -> None:
        self._calls = calls
        self._try_lock_results = try_lock_results

    async def fetchval(self, query: str, *args: object) -> bool:
        if query != "SELECT pg_try_advisory_lock($1)":
            raise AssertionError(f"unexpected query: {query}")
        self._calls.append(("try-lock", args[0]))
        return self._try_lock_results.pop(0)

    async def execute(self, query: str, *args: object) -> None:
        if query != "SELECT pg_advisory_unlock($1)":
            raise AssertionError(f"unexpected query: {query}")
        self._calls.append(("unlock", args[0]))

    async def close(self) -> None:
        self._calls.append("close")


class _FailingAdvisoryConnector:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    async def connect(self, dsn: str) -> _FailingAdvisoryConnection:
        self._calls.append(("connect", dsn))
        return _FailingAdvisoryConnection(self._calls)


class _FailingAdvisoryConnection:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    async def fetchval(self, query: str, *args: object) -> bool:
        self._calls.append(("try-lock", args[0]))
        raise RuntimeError("try-lock failed")

    async def execute(self, query: str, *args: object) -> None:
        raise AssertionError(f"unexpected unlock: {query}")

    async def close(self) -> None:
        self._calls.append("close")
