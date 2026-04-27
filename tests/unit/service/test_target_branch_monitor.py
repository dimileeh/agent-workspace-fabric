"""Target-branch integration monitor tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.service.alembic_resolver import (
    AlembicResolveResult,
    AlembicResolveStatus,
)
from awf.service.target_branch_monitor import (
    TargetBranchMonitorStatus,
    TargetBranchReconcileMonitor,
)


class _StubResolver:
    def __init__(self, result: AlembicResolveResult) -> None:
        self.result = result
        self.calls: list[Path] = []

    def resolve(self, repo_path: Path) -> AlembicResolveResult:
        self.calls.append(repo_path)
        return self.result


class _ResolvingStubResolver:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def resolve(self, repo_path: Path) -> AlembicResolveResult:
        self.calls.append(repo_path)
        return _resolved(repo_path)


def _resolved(path: Path) -> AlembicResolveResult:
    return AlembicResolveResult(
        status=AlembicResolveStatus.resolved,
        reason_code="ALEMBIC_HEADS_MERGED",
        heads=("left001", "right001"),
        generated_revision="merge001",
        generated_path=path / "migrations" / "versions" / "merge001_merge_alembic_heads.py",
    )


@pytest.mark.unit
async def test_monitor_commits_and_pushes_follow_up_merge_revision(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    runner.queue_result()  # git add
    runner.queue_result(returncode=1)  # diff --cached --quiet means staged changes
    runner.queue_result()  # commit
    runner.queue_result(stdout="abc123\n")  # rev-parse HEAD
    runner.queue_result()  # push
    resolver = _ResolvingStubResolver()
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.committed
    assert result.commit_sha == "abc123"
    assert result.pushed is True
    assert resolver.calls == [result.checkout_path]
    assert runner.calls[0].args[:4] == ["git", "clone", "--branch", "development"]
    assert any(call.args[:3] == ["git", "-C", str(result.checkout_path)] for call in runner.calls)
    commit_call = next(call for call in runner.calls if "commit" in call.args)
    assert "fix(migrations): merge Alembic heads on development" in commit_call.args
    assert runner.calls[-1].args == [
        "git",
        "-C",
        str(result.checkout_path),
        "push",
        "origin",
        "HEAD:development",
    ]


@pytest.mark.unit
async def test_monitor_noops_when_resolvers_find_no_target_branch_issue(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    resolver = _StubResolver(
        AlembicResolveResult(
            status=AlembicResolveStatus.not_needed,
            reason_code="ALEMBIC_SINGLE_HEAD",
            heads=("head001",),
        )
    )
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.clean
    assert result.pushed is False
    assert [call.args for call in runner.calls] == [
        [
            "git",
            "clone",
            "--branch",
            "development",
            "--single-branch",
            "git@github.com:example/repo.git",
            str(result.checkout_path),
        ]
    ]


@pytest.mark.unit
async def test_monitor_dry_run_reports_would_commit_without_git_write(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_StubResolver(_resolved(tmp_path)),),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
        dry_run=True,
    )

    assert result.status == TargetBranchMonitorStatus.would_commit
    assert result.pushed is False
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_monitor_refreshes_existing_checkout_before_resolving(tmp_path: Path) -> None:
    checkout = tmp_path / "target-branches" / "repo-3f11352a998e-development"
    (checkout / ".git").mkdir(parents=True)
    runner = FakeCommandRunner()
    runner.queue_result()  # fetch
    runner.queue_result()  # checkout
    runner.queue_result()  # reset
    runner.queue_result()  # clean
    resolver = _StubResolver(
        AlembicResolveResult(
            status=AlembicResolveStatus.unsupported,
            reason_code="ALEMBIC_NOT_CONFIGURED",
            heads=(),
        )
    )
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.clean
    assert resolver.calls == [checkout]
    assert [call.args for call in runner.calls] == [
        ["git", "-C", str(checkout), "fetch", "origin", "development", "--prune"],
        ["git", "-C", str(checkout), "checkout", "development"],
        ["git", "-C", str(checkout), "reset", "--hard", "origin/development"],
        ["git", "-C", str(checkout), "clean", "-fd"],
    ]
