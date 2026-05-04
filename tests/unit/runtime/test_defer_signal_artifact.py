"""Defer-signal artifact at terminal transitions.

On each terminal ``MonitorAction`` dispatch (``Merge`` /
``ShortCircuitCompleted`` / ``Abort``) the runner writes a JSON file
under ``<artifacts_root>/<workspace_id>.defer-signal.json``. ``NotifyHuman``
is a live wait state and must not publish a terminal artifact. The file
tells an orchestrator outside AWF:

  * what terminal action fired (did we actually merge?),
  * which bot-authored threads were deferred (orchestrator should spec
    follow-ups for these),
  * which human-authored threads were deferred (maintainer-owned).

Every terminal transition writes, even when both defer lists are empty —
downstream tooling shouldn't have to distinguish "no file" from "no
defers". See PR 342 Phase 3 plan.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
    thread_node,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


def _artifact_for(artifacts_root: Path, ws_id: str) -> dict:
    path = artifacts_root / f"{ws_id}.defer-signal.json"
    assert path.exists(), f"expected artifact at {path}"
    return json.loads(path.read_text())


class TestDeferSignalArtifact:
    @pytest.mark.unit
    async def test_artifact_written_on_merge_with_bot_defers(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Bot defers do NOT block the merge; artifact captures them so
        the orchestrator can spec follow-up workspaces."""
        ws_id = await seed_monitoring_workspace(factory)
        artifacts_root = tmp_path / "artifacts"
        bot_thread = thread_node(
            tid="T_bot",
            author="greptile-apps",
            path="src/foo.py",
            line=42,
            body="consider renaming",
        )
        # Outer iter 1: AddressComments fires; adapter defers.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[bot_thread]))
        adapter.queue(stdout="DEFER: advisory nit, skipping")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[bot_thread]))  # settle
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push
        # Outer iter 2: bot-defer → gate passes → Merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[bot_thread]))
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            artifacts_root=artifacts_root,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        data = _artifact_for(artifacts_root, ws_id)
        assert data["workspace_id"] == ws_id
        assert data["pr_number"] == 42
        assert data["terminal_action"] == "Merge"
        assert data["merged"] is True
        assert len(data["deferred_bot_items"]) == 1
        item = data["deferred_bot_items"][0]
        assert item["kind"] == "thread"
        assert item["id"] == "T_bot"
        assert item["author"] == "greptile-apps"
        assert item["path"] == "src/foo.py"
        assert item["line"] == 42
        assert data["deferred_human_items"] == []

    @pytest.mark.unit
    async def test_merge_blocked_notification_waits_until_external_merge_for_artifact(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """``gh pr merge`` can be rejected by branch protection.

        The runner posts a human-attention comment, but does not publish a
        terminal artifact or tear down the workspace until the PR is
        actually merged.
        """
        ws_id = await seed_monitoring_workspace(factory)
        artifacts_root = tmp_path / "artifacts"
        bot_thread = thread_node(
            tid="T_bot",
            author="greptile-apps",
            path="src/foo.py",
            line=42,
            body="consider renaming",
        )
        # Outer iter 1: AddressComments fires; adapter defers.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[bot_thread]))
        adapter.queue(stdout="DEFER: advisory nit, skipping")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[bot_thread]))  # settle
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push
        # Outer iter 2: bot-defer → gate passes → Merge dispatched, but
        # gh pr merge is blocked by branch protection → downgrade path.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[bot_thread]))
        cmd.queue_result(
            returncode=1,
            stderr="pull request not mergeable: required status checks pending",
        )  # gh pr merge — blocked
        cmd.queue_result(returncode=0)  # gh pr comment (ready-to-merge fallback)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True, threads=[bot_thread]))
        cmd.queue_result(returncode=0)  # docker compose down
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            artifacts_root=artifacts_root,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        data = _artifact_for(artifacts_root, ws_id)
        assert data["terminal_action"] == "ShortCircuitCompleted"
        assert data["merged"] is True
        assert len(data["deferred_bot_items"]) == 1
        item = data["deferred_bot_items"][0]
        assert item["id"] == "T_bot"
        assert item["author"] == "greptile-apps"
        assert item["path"] == "src/foo.py"
        assert item["line"] == 42
        assert data["deferred_human_items"] == []

    @pytest.mark.unit
    async def test_human_defer_notification_waits_until_external_merge_for_artifact(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        artifacts_root = tmp_path / "artifacts"
        human_thread = thread_node(
            tid="T_human",
            author="alice-human",
            path="src/bar.py",
            line=7,
            body="design question",
        )
        # Outer iter 1: AddressComments; agent defers.
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[human_thread]))
        adapter.queue(stdout="DEFER: needs maintainer input")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[human_thread]))
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")
        # Outer iter 2: human-defer → gate blocks → NotifyHuman.
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[human_thread]))
        cmd.queue_result(returncode=0)  # gh pr comment (NotifyHuman)
        # NotifyHuman is not terminal; the monitor remains alive until the
        # PR is actually merged.
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True, threads=[human_thread]))
        cmd.queue_result(returncode=0)  # docker compose down
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            artifacts_root=artifacts_root,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        data = _artifact_for(artifacts_root, ws_id)
        assert data["terminal_action"] == "ShortCircuitCompleted"
        assert data["merged"] is True
        assert data["deferred_bot_items"] == []
        assert len(data["deferred_human_items"]) == 1
        item = data["deferred_human_items"][0]
        assert item["id"] == "T_human"
        assert item["author"] == "alice-human"
        assert item["path"] == "src/bar.py"

    @pytest.mark.unit
    async def test_artifact_written_on_abort(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        artifacts_root = tmp_path / "artifacts"
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(closed=True))  # Abort path
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            artifacts_root=artifacts_root,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        data = _artifact_for(artifacts_root, ws_id)
        assert data["terminal_action"] == "Abort"
        assert data["merged"] is False
        assert data["deferred_bot_items"] == []
        assert data["deferred_human_items"] == []

    @pytest.mark.unit
    async def test_artifact_written_on_short_circuit_already_merged(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Even on ShortCircuitCompleted (PR merged externally), write
        the artifact. Orchestrator shouldn't have to handle missing
        files when polling for terminal signals."""
        ws_id = await seed_monitoring_workspace(factory)
        artifacts_root = tmp_path / "artifacts"
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            artifacts_root=artifacts_root,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        data = _artifact_for(artifacts_root, ws_id)
        assert data["terminal_action"] == "ShortCircuitCompleted"
        assert data["deferred_bot_items"] == []
        assert data["deferred_human_items"] == []

    @pytest.mark.unit
    async def test_artifact_default_path_under_worktrees_parent(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """When ``artifacts_root`` is not passed to the runner, it
        defaults to ``worktrees_root.parents[1] / "artifacts"`` — matches
        the layout ``run_awf.py`` uses where ``worktrees_root`` is
        ``<work_dir>/git/worktrees`` and artifacts live at
        ``<work_dir>/artifacts``."""
        ws_id = await seed_monitoring_workspace(factory)
        worktrees_root = tmp_path / "work" / "git" / "worktrees"
        default_artifacts_root = tmp_path / "work" / "artifacts"
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=worktrees_root,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        path = default_artifacts_root / f"{ws_id}.defer-signal.json"
        assert path.exists(), f"expected default artifact at {path}"
