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
    TargetBranchMonitorError,
    TargetBranchMonitorStatus,
    TargetBranchReconcileMonitor,
    run_target_branch_reconcile_once,
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
    assert result.to_dict()["status"] == "clean"
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
        resolvers=(_ResolvingStubResolver(),),
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


@pytest.mark.unit
async def test_existing_non_git_checkout_raises_before_resolver_or_git(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "target-branches" / "repo-3f11352a998e-development"
    checkout.mkdir(parents=True)
    runner = FakeCommandRunner()
    resolver = _StubResolver(_resolved(tmp_path))
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    with pytest.raises(RuntimeError, match="not a git checkout"):
        await monitor.reconcile(
            repo_url="git@github.com:example/repo.git",
            branch="development",
        )

    assert runner.calls == []
    assert resolver.calls == []


@pytest.mark.unit
async def test_monitor_returns_clean_when_resolver_change_has_no_staged_diff(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    runner.queue_result()  # add generated file
    runner.queue_result(returncode=0)  # diff --cached --quiet: no staged changes
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_ResolvingStubResolver(),),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.clean
    assert result.pushed is False
    assert runner.calls[0].args[1] == "clone"
    assert runner.calls[1].args[3] == "add"
    assert runner.calls[2].args[3:6] == ["diff", "--cached", "--quiet"]


@pytest.mark.unit
async def test_monitor_raises_when_cached_diff_returns_unexpected_code(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    runner.queue_result()  # add
    runner.queue_result(returncode=2, stderr="diff failed")
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_ResolvingStubResolver(),),
    )

    with pytest.raises(TargetBranchMonitorError) as exc_info:
        await monitor.reconcile(
            repo_url="git@github.com:example/repo.git",
            branch="development",
        )

    assert exc_info.value.operation == "target_branch.git_diff_cached"
    assert "diff failed" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("existing_checkout", "operation"),
    [
        (False, "target_branch.git_clone"),
        (True, "target_branch.git_fetch"),
    ],
)
async def test_monitor_command_failures_report_operation_names(
    tmp_path: Path,
    existing_checkout: bool,
    operation: str,
) -> None:
    checkout = tmp_path / "target-branches" / "repo-3f11352a998e-development"
    if existing_checkout:
        (checkout / ".git").mkdir(parents=True)
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="git failed")
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_StubResolver(_resolved(tmp_path)),),
    )

    with pytest.raises(TargetBranchMonitorError) as exc_info:
        await monitor.reconcile(
            repo_url="git@github.com:example/repo.git",
            branch="development",
        )

    assert exc_info.value.operation == operation
    assert "git failed" in str(exc_info.value)


@pytest.mark.unit
async def test_checkout_path_slug_fallbacks_and_convenience_entrypoint(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(
            _StubResolver(
                AlembicResolveResult(
                    status=AlembicResolveStatus.not_needed,
                    reason_code="ALEMBIC_SINGLE_HEAD",
                    heads=("head",),
                )
            ),
        ),
    )
    fallback_path = monitor._checkout_path(repo_url="///", branch="///")

    result = await run_target_branch_reconcile_once(
        runner=runner,
        work_dir=tmp_path,
        repo_url="git@github.com:example/repo.git",
        branch="development",
        dry_run=True,
    )

    assert fallback_path.name.startswith("repo-")
    assert fallback_path.name.endswith("-branch")
    assert result.status == TargetBranchMonitorStatus.clean
