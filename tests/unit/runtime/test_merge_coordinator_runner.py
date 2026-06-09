"""PR monitor integration points for merge coordination."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import Merge, MonitorState
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    mock_completed_compose_manager,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status

REPO_URL = "git@github.com:dimileeh/aira-web.git"


class RecordingMergeCoordinator:
    def __init__(self) -> None:
        self.active = False
        self.entries: list[tuple[str, str]] = []

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        if self.active:
            raise AssertionError("test coordinator was entered recursively")
        self.entries.append((repo_url, base_branch))
        self.active = True
        try:
            yield
        finally:
            self.active = False


class FailOnUseMergeCoordinator:
    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        raise AssertionError(f"unexpected merge lock for {repo_url}@{base_branch}")
        yield


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


class TestMergeCoordinatorRunner:
    @pytest.mark.unit
    async def test_auto_merge_wraps_final_recheck_and_merge_in_coordinator(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        coordinator = RecordingMergeCoordinator()

        # Initial poll: mergeable. Final recheck under the coordinator:
        # still mergeable, then gh pr merge runs.
        cmd.queue_result(returncode=0)  # initial git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # initial base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # initial PR state
        cmd.queue_result(returncode=0)  # final git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # final base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # final PR state
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # merge SHA lookup

        observed: list[tuple[list[str], bool]] = []
        original_run = cmd.run

        async def tracking_run(args, **kwargs):  # type: ignore[no-untyped-def]
            observed.append((list(args), coordinator.active))
            return await original_run(args, **kwargs)

        cmd.run = tracking_run  # type: ignore[method-assign]

        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            pre_merge_settle_seconds=9,
            merge_coordinator=coordinator,
        )

        teardown_active: list[bool] = []
        compose_patch, compose_calls = mock_completed_compose_manager(
            on_teardown=lambda: teardown_active.append(coordinator.active)
        )
        with (
            structlog.testing.capture_logs() as captured,
            compose_patch,
        ):
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        assert coordinator.entries == [(REPO_URL, "development")]
        assert sleep_fn.calls == [9]

        graphql_active = [
            active for args, active in observed if args[:3] == ["gh", "api", "graphql"]
        ]
        assert graphql_active == [False, True]
        assert [active for args, active in observed if args[:3] == ["gh", "pr", "merge"]] == [True]
        assert not any(args[:2] == ["docker", "compose"] for args, _active in observed)
        assert compose_calls == [
            (f"awf_{ws_id}", Path(f"/tmp/awf/{ws_id}/compose.yml"), ws_id, True)
        ]
        assert teardown_active == [False]

        waiting = [
            r for r in captured if r.get("event") == "monitor.merge_critical_section_waiting"
        ]
        entered = [
            r for r in captured if r.get("event") == "monitor.merge_critical_section_entered"
        ]
        assert len(waiting) == 1
        assert len(entered) == 1
        assert waiting[0]["workspace_id"] == ws_id
        assert waiting[0]["repo_url"] == REPO_URL
        assert waiting[0]["base_branch"] == "development"
        assert entered[0]["workspace_id"] == ws_id
        assert entered[0]["pr_number"] == 42

    @pytest.mark.unit
    async def test_final_recheck_base_drift_falls_back_to_sync_base_outside_lock(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        coordinator = RecordingMergeCoordinator()

        # Recheck sees base drift and the recursive SyncBase path should
        # run after the merge lock has been released.
        cmd.queue_result(returncode=0)  # final git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="1\n")  # final base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # final PR state
        cmd.queue_result(returncode=0)  # sync-base git merge --abort
        cmd.queue_result(returncode=0)  # sync-base git fetch origin <base>
        cmd.queue_result(returncode=0)  # sync-base git merge --no-edit origin/<base>
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push

        observed: list[tuple[list[str], bool]] = []
        original_run = cmd.run

        async def tracking_run(args, **kwargs):  # type: ignore[no-untyped-def]
            observed.append((list(args), coordinator.active))
            return await original_run(args, **kwargs)

        cmd.run = tracking_run  # type: ignore[method-assign]

        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            pre_merge_settle_seconds=5,
            merge_coordinator=coordinator,
        )

        terminal = await runner._execute(
            action=Merge(),
            workspace_id=ws_id,
            repo_url=REPO_URL,
            repo=RepoRef.from_url(REPO_URL),
            pr_number=42,
            status=replace(_status(), head_sha="abc1234567890def"),
            state=MonitorState(),
            base_branch="development",
            remote_branch=f"awf/{ws_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

        assert terminal is False
        assert coordinator.entries == [(REPO_URL, "development")]
        assert sleep_fn.calls == [5]
        assert not any(args[:3] == ["gh", "pr", "merge"] for args, _active in observed)

        sync_commands = [
            (args, active)
            for args, active in observed
            if (
                ("merge" in args and "--abort" in args)
                or ("merge" in args and "--no-edit" in args)
                or ("push" in args)
            )
        ]
        assert sync_commands
        assert all(active is False for _args, active in sync_commands)

    @pytest.mark.unit
    async def test_manual_auto_merge_false_monitoring_never_enters_coordinator(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)

        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # green PR
        cmd.queue_result(returncode=0)  # gh pr comment
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        cmd.queue_result(returncode=0)  # docker compose down

        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            auto_merge=False,
            merge_coordinator=FailOnUseMergeCoordinator(),
        )

        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        assert sleep_fn.calls == [60]
