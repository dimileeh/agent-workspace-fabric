"""Unit tests for the in-process merge coordinator.

The coordinator is deliberately tiny today: it serializes merge attempts
inside one worker process by ``repo_url + base_branch``. The tests keep
that contract explicit so a future Postgres advisory-lock implementation
can replace it without changing monitor-runner behavior.
"""

from __future__ import annotations

import asyncio

import pytest

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
