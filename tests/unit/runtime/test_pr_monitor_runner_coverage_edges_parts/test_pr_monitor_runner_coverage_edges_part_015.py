"""Focused branch-coverage tests for PR monitor runner edge behavior. (split part)"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


_PROTECTED_WORKFLOW_OLD = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff
        run: uv run ruff check
""".strip()
_PROTECTED_WORKFLOW_BLOCKED = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()


def _queue_protected_workflow_diff(
    cmd: FakeCommandRunner,
    *,
    old_text: str = _PROTECTED_WORKFLOW_OLD,
    new_text: str = _PROTECTED_WORKFLOW_BLOCKED,
) -> None:
    cmd.queue_result(returncode=0)  # cat-file base:path
    cmd.queue_result(returncode=0, stdout=old_text)
    cmd.queue_result(returncode=0)  # cat-file HEAD:path
    cmd.queue_result(returncode=0, stdout=new_text)


def _assert_committed_diff_phase_ran(
    cmd: FakeCommandRunner,
    *,
    worktree_path: Path,
    remote_branch: str,
    remote: str = "origin",
) -> None:
    call_args = [call.args for call in cmd.calls]
    assert (
        _git_worktree_command(
            worktree_path,
            "fetch",
            remote,
            f"refs/heads/{remote_branch}",
        )
        in call_args
    )
    assert (
        _git_worktree_command(
            worktree_path,
            "merge-base",
            "FETCH_HEAD",
            "HEAD",
        )
        in call_args
    )


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


@pytest.mark.unit
async def test_sync_base_blocks_committed_protected_quality_gate_edits_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch base
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml", "src/fix.py"))
    cmd.queue_result(returncode=0, stdout="")  # refresh base branch for sync-base diff
    cmd.queue_result(returncode=0, stdout="merged-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml", "src/fix.py"))
    _queue_protected_workflow_diff(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    push_result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url="https://github.com/org/fork.git",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert ".github/workflows/ci.yml" in push_result.stderr
    call_args = [call.args for call in cmd.calls]
    assert any(
        args[:1] == ["git"]
        and "fetch" in args
        and "https://github.com/org/fork.git" in args
        and f"refs/heads/awf/{workspace_id}" in args
        for args in call_args
    )
    assert any(
        args[:1] == ["git"]
        and "diff" in args
        and "--name-status" in args
        and "-z" in args
        and "merged-base-sha..HEAD" in args
        for args in call_args
    )
    assert not any(args[:1] == ["git"] and "push" in args for args in call_args)
    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_push_blocked",
            limit=10,
        )
    assert len(events) == 1
    assert events[0].reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert events[0].payload is not None
    assert events[0].payload["paths"] == [".github/workflows/ci.yml"]


@pytest.mark.unit
async def test_protected_scope_push_check_allows_safe_pinned_workflow_uses_bump(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4.2.0
      - name: Run pytest
        run: uv run pytest
""".strip()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml"))
    _queue_protected_workflow_diff(cmd, old_text=old_text, new_text=new_text)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    block = await runner._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )

    assert block is None
    call_args = [call.args for call in cmd.calls]
    assert any(
        args[:1] == ["git"] and "show" in args and "merge-base-sha:.github/workflows/ci.yml" in args
        for args in call_args
    )


@pytest.mark.unit
async def test_sync_base_allows_base_owned_protected_quality_gate_changes_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch base
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml"))
    cmd.queue_result(returncode=0, stdout="")  # refresh base branch for sync-base diff
    cmd.queue_result(returncode=0, stdout="merged-base-sha\n")
    cmd.queue_result(returncode=0, stdout="")  # diff against merged base excludes base changes
    cmd.queue_result(returncode=0, stdout="", stderr="pushed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    push_result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.failed is False
    assert push_result.pushed is True
    _assert_committed_diff_phase_ran(
        cmd,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )
    call_args = [call.args for call in cmd.calls]
    assert any(
        args[:1] == ["git"]
        and "diff" in args
        and "--name-status" in args
        and "-z" in args
        and "merged-base-sha..HEAD" in args
        for args in call_args
    )
    assert any(args[:1] == ["git"] and "push" in args for args in call_args)
    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_push_blocked",
            limit=10,
        )
    assert events == []


@pytest.mark.unit
async def test_sync_base_allows_base_owned_protected_changes_when_base_advances_again(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    class AdvancingBaseRunner(FakeCommandRunner):
        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            await super().run(args, input_bytes=input_bytes, cwd=cwd)
            if args[-1:] == [f"refs/heads/awf/{workspace_id}"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            if args[-2:] == ["FETCH_HEAD", "HEAD"]:
                return CommandResult(returncode=0, stdout="remote-branch-base-sha\n", stderr="")
            if "remote-branch-base-sha..HEAD" in args:
                return CommandResult(
                    returncode=0,
                    stdout=_name_status_z(".github/workflows/ci.yml"),
                    stderr="",
                )
            if args[-1:] == ["+refs/heads/development:refs/remotes/origin/development"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            if args[-2:] == ["origin/development", "HEAD"]:
                return CommandResult(returncode=0, stdout="merged-base-sha\n", stderr="")
            if "merged-base-sha..HEAD" in args:
                return CommandResult(returncode=0, stdout="", stderr="")
            if "origin/development..HEAD" in args:
                return CommandResult(
                    returncode=0,
                    stdout=_name_status_z(".github/workflows/ci.yml"),
                    stderr="",
                )
            return CommandResult(returncode=0, stdout="", stderr="")

    cmd = AdvancingBaseRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    violations = await runner._protected_scope_violations_for_sync_base_push(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktree",
        remote_branch=f"awf/{workspace_id}",
        base_branch="development",
    )

    assert violations == []
    call_args = [call.args for call in cmd.calls]
    worktree = tmp_path / "worktree"
    assert (
        _git_worktree_command(
            worktree,
            "diff",
            "--name-status",
            "-z",
            "origin/development..HEAD",
            "--",
        )
        not in call_args
    )
    assert (
        _git_worktree_command(
            worktree,
            "diff",
            "--name-status",
            "-z",
            "merged-base-sha..HEAD",
            "--",
        )
        in call_args
    )


@pytest.mark.unit
async def test_ci_fix_commits_and_pushes_even_if_agent_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="format failed")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    for result in [
        (0, "", ""),  # clean worktree before repair
        (0, "abc1234567890def\n", ""),  # operation start HEAD
        (0, " M tests/test_app.py\n", ""),  # commit_dirty status --porcelain
        (0, " M tests/test_app.py\n", ""),  # commit_dirty status --untracked-files=all
        (0, "", ""),
        (1, "", ""),
        (0, "", ""),
        (0, "", ""),  # fetch remote branch for committed diff
        (0, "merge-base-sha\n", ""),
        (
            0,
            _name_status_z("tests/test_app.py"),
            "",
        ),  # committed diff is inside ordinary test files
        (0, "", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="assert 1 == 2"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert len(adapter.calls) == 1
    assert "assert 1 == 2" in adapter.calls[0]
    _assert_committed_diff_phase_ran(
        cmd,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )
    assert cmd.calls[-1].args[-2:] == ["origin", f"HEAD:refs/heads/awf/{workspace_id}"]
