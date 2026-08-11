"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

import asyncio
import fcntl
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.companions import isolated_reask_worktree_liveness_lock_path
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime
from awf.node.git_manager import GitOperationError
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor_runner import agent_service_recovery, comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.service import gc_reconcile
from awf.service.gc_reconcile import is_active_isolated_reask_worktree
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    scan_managed_worktrees,
)


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real git command in a temporary worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_real_worktree(tmp_path: Path, workspace_id: str) -> Path:
    """Create a committed worktree suitable for the real re-ask cleanup path."""
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / ".gitignore").write_text("*.env\n", encoding="utf-8")
    (worktree / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore", "tracked.py")
    _git(worktree, "commit", "-qm", "initial")
    return worktree


class _LocalCommandRunner:
    """Run the PR monitor's git commands against a temporary real worktree."""

    async def run(self, args: list[str]) -> CommandResult:
        """Run this test double and record the invocation."""
        proc = subprocess.run(args, capture_output=True, text=True)
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


@pytest.mark.unit
async def test_reask_worktree_is_passed_to_the_agent_adapter(tmp_path: Path) -> None:
    """Recovery preserves the one-off mount request through to the local adapter."""
    calls: list[dict[str, object]] = []

    class _Adapter:
        """Test double used by the surrounding scenario."""

        is_hosted = False

        async def run(self, **kwargs: object) -> AgentRunResult:
            """Run this test double and record the invocation."""
            calls.append(dict(kwargs))
            return AgentRunResult(
                returncode=0, stdout="AWF-VERDICT: NEEDS_HUMAN: reason", stderr=""
            )

    reask_worktree = tmp_path / ".awf-needs-human-reask-test"
    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))
    result = await agent_service_recovery._run_monitor_agent_with_service_recovery(
        runner,
        workspace_id="ws_reask",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        prompt="state the reason",
        log_source="recovery",
        isolated_worktree_host_path=reask_worktree,
    )

    assert result.stdout.endswith("reason")
    assert calls == [
        {
            "compose_project": "awf_ws_reask",
            "compose_file": tmp_path / "compose.yml",
            "prompt": "state the reason",
            "workspace_id": "ws_reask",
            "log_source": "recovery",
            "isolated_worktree_host_path": reask_worktree,
        }
    ]


@pytest.mark.unit
async def test_isolated_reask_agent_error_does_not_restart_persistent_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason-only re-ask makes one attempt, even when the agent times out."""
    calls = 0

    class _Adapter:
        """Test double used by the surrounding scenario."""

        is_hosted = False

        async def run(self, **_kwargs: object) -> AgentRunResult:
            """Run this test double and record the invocation."""
            nonlocal calls
            calls += 1
            raise AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(returncode=1, stdout="", stderr="timed out"),
                reason_code="AGENT_TIMEOUT",
            )

    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))

    async def _unexpected_recovery(_runner: object, **_kwargs: object) -> None:
        """Fail if recovery is invoked."""
        raise AssertionError("isolated clarification must not enter service recovery")

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_error",
        _unexpected_recovery,
    )

    with pytest.raises(AgentRunError, match="timed out"):
        await agent_service_recovery._run_monitor_agent_with_service_recovery(
            runner,
            workspace_id="ws_reask",
            compose_project="awf_ws_reask",
            compose_file=tmp_path / "compose.yml",
            prompt="state the reason",
            log_source="recovery",
            isolated_worktree_host_path=tmp_path / ".awf-needs-human-reask-test",
        )

    assert calls == 1


@pytest.mark.unit
async def test_isolated_reask_cleanup_error_does_not_restart_persistent_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason-only re-ask does not retry after isolated-run cleanup fails."""
    calls = 0

    class _Adapter:
        """Test double used by the surrounding scenario."""

        is_hosted = False

        async def run(self, **_kwargs: object) -> AgentRunResult:
            """Run this test double and record the invocation."""
            nonlocal calls
            calls += 1
            raise ComposeExecCleanupError(
                invocation_id="reask",
                source="agent",
                label="clarification",
                message="cleanup failed",
            )

    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))

    async def _unexpected_recovery(_runner: object, **_kwargs: object) -> None:
        """Fail if recovery is invoked."""
        raise AssertionError("isolated clarification must not enter service recovery")

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_cleanup_error",
        _unexpected_recovery,
    )

    with pytest.raises(ComposeExecCleanupError, match="cleanup failed"):
        await agent_service_recovery._run_monitor_agent_with_service_recovery(
            runner,
            workspace_id="ws_reask",
            compose_project="awf_ws_reask",
            compose_file=tmp_path / "compose.yml",
            prompt="state the reason",
            log_source="recovery",
            isolated_worktree_host_path=tmp_path / ".awf-needs-human-reask-test",
        )

    assert calls == 1


@pytest.mark.unit
async def test_isolated_reask_worktree_is_sibling_and_excludes_ignored_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clarification checkout is a sibling with tracked source only."""
    worktree_root = tmp_path / "git" / "worktrees"
    worktree_root.mkdir(parents=True)
    worktree = _init_real_worktree(worktree_root, "ws_reask_isolation")
    (worktree / ".gitignore").write_text("*.env\n.venv/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore dependencies")
    dependency = worktree / ".venv" / "lib" / "dependency.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("large dependency tree\n", encoding="utf-8")
    scratch = worktree / ".agent-scratch" / "session.txt"
    scratch.parent.mkdir()
    scratch.write_text("runtime state\n", encoding="utf-8")
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=_LocalCommandRunner(),
            adapter=SimpleNamespace(runtime_scratch_paths=(".agent-scratch/",)),
        )
    )
    ownership_repairs: list[dict[str, object]] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        """Record the synthetic agent-worktree ownership repair."""
        ownership_repairs.append(dict(kwargs))
        return True

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
    )

    assert reask_worktree is not None
    assert reask_worktree.path.parent == worktree.parent
    assert reask_worktree.path.name.startswith("ws_reask_isolation__companion__isolated_reask_")
    assert reask_worktree.liveness_lock_path == isolated_reask_worktree_liveness_lock_path(
        reask_worktree.path
    )
    assert is_active_isolated_reask_worktree(reask_worktree.path)
    scan = scan_managed_worktrees(tmp_path)
    assert (str(reask_worktree.path), "ws_reask_isolation") in {
        (resource.path, resource.workspace_id) for resource in scan.resources
    }
    assert (
        build_orphan_resource_summary(
            docker_scan=empty_docker_scan(),
            worktree_scan=scan,
            workspace_view=WorkspaceIdView(
                active_ids=frozenset({"ws_reask_isolation"}),
                terminal_ids=frozenset(),
                available=True,
            ),
        ).reason
        == "NO_ORPHANS"
    )
    assert not list(worktree.glob("*__companion__isolated_reask_*"))
    assert _git(worktree, "status", "--porcelain", "--untracked-files=all").stdout == ""
    assert (reask_worktree.path / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (reask_worktree.path / ".venv").exists()
    assert not (reask_worktree.path / ".agent-scratch").exists()
    assert dependency.exists()
    assert scratch.exists()
    assert ownership_repairs == [
        {
            "logger": comments._log,
            "workspace_id": "ws_reask_isolation",
            "worktree_path": reask_worktree.path,
            "reason": "needs_human_reason_reask_pre_launch",
            "event_name": "monitor.agent_runtime_ownership_repair_failed",
            "linked_worktree_id": reask_worktree.path.name,
        }
    ]

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None
    assert not reask_worktree.path.exists()
    assert not reask_worktree.liveness_lock_path.exists()


@pytest.mark.unit
async def test_isolated_reask_worktree_disables_primary_post_checkout_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-ask checkout cannot execute a hook the previous agent left in the mirror."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_hooks_disabled")
    post_checkout = worktree / ".git" / "hooks" / "post-checkout"
    post_checkout.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    post_checkout.chmod(0o755)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Avoid changing ownership while exercising the Git invocation."""
        return True

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
    )

    assert reask_worktree is not None
    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None


@pytest.mark.unit
async def test_isolated_reask_worktree_disables_primary_checkout_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-ask checkout cannot run a filter configured by the previous agent."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_filters_disabled")
    (worktree / ".gitattributes").write_text("filtered.txt filter=poison\n", encoding="utf-8")
    (worktree / "filtered.txt").write_text("original content\n", encoding="utf-8")
    _git(worktree, "add", ".gitattributes", "filtered.txt")
    _git(worktree, "commit", "-qm", "add filtered file")
    filter_marker = tmp_path / "smudge-filter-ran"
    _git(
        worktree,
        "config",
        "filter.poison.smudge",
        f"touch '{filter_marker}'; cat",
    )
    _git(worktree, "config", "filter.poison.clean", "cat")
    _git(worktree, "config", "filter.poison.required", "true")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Avoid changing ownership while exercising the Git invocation."""
        return True

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
    )

    assert reask_worktree is not None
    assert not filter_marker.exists()
    assert (reask_worktree.path / "filtered.txt").read_text(
        encoding="utf-8"
    ) == "original content\n"
    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None


@pytest.mark.unit
async def test_checkout_filter_overrides_fail_closed_when_filter_probe_fails() -> None:
    """An unreadable filter configuration cannot lead to an unsafe checkout."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=2, stderr="config unreadable")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not determine checkout filters"):
        await comments._checkout_filter_overrides(runner, worktree_path=Path("/worktree"))


@pytest.mark.unit
async def test_checkout_filter_overrides_reject_unexpected_config_key() -> None:
    """A malformed filter key cannot be passed into the host Git command."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=0, stdout="filter.poison/unsafe.smudge\n")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(
        _MonitorPolicyBlockedError, match="Could not safely disable checkout filters"
    ):
        await comments._checkout_filter_overrides(runner, worktree_path=Path("/worktree"))


@pytest.mark.unit
async def test_isolated_reask_worktree_is_removed_when_ownership_repair_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout that cannot be prepared for the agent is not mounted or retained."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_ownership_failure")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Record the synthetic agent-worktree ownership repair."""
        return False

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    with pytest.raises(
        _MonitorPolicyBlockedError, match="Could not repair isolated worktree"
    ) as exc_info:
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert exc_info.value.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_blocks_when_ownership_failure_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ownership repair does not hide a failed isolated-checkout cleanup."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_ownership_cleanup_failure")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))
    cleanup = comments._cleanup_isolated_reask_worktree_after_creation_failure

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Record the synthetic agent-worktree ownership repair."""
        return False

    async def _cleanup_isolated_reask_worktree_after_creation_failure(
        cleanup_runner: object,
        **kwargs: object,
    ) -> str:
        """Exercise the _cleanup_isolated_reask_worktree_after_creation_failure test helper."""
        assert await cleanup(cleanup_runner, **kwargs) is None
        return "simulated cleanup failure"

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)
    monkeypatch.setattr(
        comments,
        "_cleanup_isolated_reask_worktree_after_creation_failure",
        _cleanup_isolated_reask_worktree_after_creation_failure,
    )

    with pytest.raises(_MonitorPolicyBlockedError, match="simulated cleanup failure"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_preserves_dirty_primary_worktree(tmp_path: Path) -> None:
    """A clarification checkout must not turn pre-existing primary-worktree edits into cleanup."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_dirty_primary")
    (worktree / "preexisting.txt").write_text("do not delete\n", encoding="utf-8")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not prepare an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert (worktree / "preexisting.txt").read_text(encoding="utf-8") == "do not delete\n"


@pytest.mark.unit
async def test_isolated_reask_worktree_preserves_empty_primary_worktree_dir(
    tmp_path: Path,
) -> None:
    """A cleanliness preflight must not delete unrelated empty directories."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_empty_primary")
    empty_output_dir = worktree / "operator-output"
    empty_output_dir.mkdir()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not prepare an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert empty_output_dir.exists()


@pytest.mark.unit
async def test_isolated_reask_worktree_releases_liveness_lock_when_git_add_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thrown Git add error cannot leave a live-GC marker behind."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_add_raises")
    lock_paths: list[Path] = []
    acquire_lock = comments._acquire_isolated_reask_liveness_lock

    class _GitAddRaisesRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "add" in args:
                raise RuntimeError("worktree add failed")
            return await super().run(args)

    def _record_lock(path: Path) -> tuple[int, Path]:
        """Record lock for this test."""
        lock_fd, lock_path = acquire_lock(path)
        lock_paths.append(lock_path)
        return lock_fd, lock_path

    monkeypatch.setattr(comments, "_acquire_isolated_reask_liveness_lock", _record_lock)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_GitAddRaisesRunner()))

    with pytest.raises(RuntimeError, match="worktree add failed"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert lock_paths
    assert not lock_paths[0].exists()


@pytest.mark.unit
def test_reask_liveness_acquisition_rejects_marker_reaped_before_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GC cannot leave a monitor holding an unlinked pre-checkout marker."""
    path = (
        tmp_path
        / "git"
        / "worktrees"
        / "ws_reask_race__companion__isolated_reask_0123456789abcdef0123456789abcdef"
    )
    lock_path = isolated_reask_worktree_liveness_lock_path(path)
    real_flock = fcntl.flock
    reaped_before_monitor_lock = False

    def _race_flock(lock_fd: int, operation: int) -> None:
        nonlocal reaped_before_monitor_lock
        if not reaped_before_monitor_lock and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            reaped_before_monitor_lock = True
            gc_reconcile._reap_stale_pre_checkout_isolated_reask_liveness_locks(tmp_path)
        real_flock(lock_fd, operation)

    monkeypatch.setattr(comments.fcntl, "flock", _race_flock)

    with pytest.raises(FileNotFoundError):
        comments._acquire_isolated_reask_liveness_lock(path)

    assert reaped_before_monitor_lock
    assert not lock_path.exists()


@pytest.mark.unit
def test_reask_liveness_acquisition_preserves_replacement_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed monitor must not unlink a marker that replaced its own."""
    path = (
        tmp_path
        / "git"
        / "worktrees"
        / "ws_reask_race__companion__isolated_reask_0123456789abcdef0123456789abcdef"
    )
    lock_path = isolated_reask_worktree_liveness_lock_path(path)
    real_flock = fcntl.flock
    replacement_created = False

    def _replace_marker_after_lock(lock_fd: int, operation: int) -> None:
        nonlocal replacement_created
        real_flock(lock_fd, operation)
        if not replacement_created and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            replacement_created = True
            lock_path.unlink()
            lock_path.write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(comments.fcntl, "flock", _replace_marker_after_lock)

    with pytest.raises(OSError, match="marker was replaced"):
        comments._acquire_isolated_reask_liveness_lock(path)

    assert replacement_created
    assert lock_path.read_text(encoding="utf-8") == "replacement"


@pytest.mark.unit
async def test_isolated_reask_worktree_blocks_when_liveness_lock_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification does not start without the lock that makes GC safe."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_lock_unavailable")

    def _lock_unavailable(_fd: int, _operation: int) -> None:
        """Exercise the _lock_unavailable test helper."""
        raise OSError("advisory locks unavailable")

    monkeypatch.setattr(comments.fcntl, "flock", _lock_unavailable)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not protect"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))
    assert not list((worktree.parent / ".awf-isolated-reask-locks").iterdir())


@pytest.mark.unit
async def test_isolated_reask_worktree_creation_failure_blocks_clarification(
    tmp_path: Path,
) -> None:
    """Do not start a re-ask when Git cannot create its isolated checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_failure")
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=0)  # primary-worktree status
    command_runner.queue_result(returncode=1)  # no configured checkout filters
    command_runner.queue_result(returncode=1, stderr="worktree add failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not create an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert "worktree" in command_runner.calls[2].args
    assert "add" in command_runner.calls[2].args


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_after_nonzero_creation_result(
    tmp_path: Path,
) -> None:
    """A failed worktree-add result cannot leave its populated checkout behind."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_nonzero_after_checkout")

    class _NonzeroAfterWorktreeAddRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr="post-checkout hook failed",
                )
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_NonzeroAfterWorktreeAddRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not create an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_blocks_when_creation_cleanup_fails(
    tmp_path: Path,
) -> None:
    """A failed cleanup after a failed add cannot be treated as advisory setup."""
    workspace_id = "ws_reask_create_cleanup_failure"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    audit_events: list[dict[str, object]] = []

    class _NonzeroAfterWorktreeAddWithFailedCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr="post-checkout hook failed",
                )
            return result

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_NonzeroAfterWorktreeAddWithFailedCleanupRunner()),
        _worktrees_root=tmp_path,
        _rev_parse_head=_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(
        _MonitorPolicyBlockedError,
        match="git worktree remove.*could not remove",
    ) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert audit_events == []
    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "cleanup_error",
    (
        GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr="worktree remove failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        ),
        OSError("worktree remove unavailable"),
        RuntimeError("worktree remove failed"),
    ),
)
async def test_needs_human_reason_reask_blocks_when_creation_cleanup_raises(
    tmp_path: Path,
    cleanup_error: Exception,
) -> None:
    """A failed-add checkout removal exception cannot become an advisory setup failure."""
    workspace_id = "ws_reask_create_cleanup_exception"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    audit_events: list[dict[str, object]] = []

    class _NonzeroAfterWorktreeAddWithExceptionalCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                raise cleanup_error
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr="post-checkout hook failed",
                )
            return result

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_NonzeroAfterWorktreeAddWithExceptionalCleanupRunner()),
        _worktrees_root=tmp_path,
        _rev_parse_head=_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert audit_events == []
    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_when_creation_is_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation after Git creates the checkout cannot strand the re-ask worktree."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_cancelled")

    class _CancelAfterWorktreeAddRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_CancelAfterWorktreeAddRunner()))

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_when_ownership_repair_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during ownership repair cannot strand the re-ask worktree."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_ownership_repair_cancelled")
    repair_started = asyncio.Event()
    lock_fds: list[int] = []
    acquire_lock = comments._acquire_isolated_reask_liveness_lock

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Record the synthetic agent-worktree ownership repair."""
        repair_started.set()
        await asyncio.Event().wait()
        return True

    def _record_lock(path: Path) -> tuple[int, Path]:
        """Record lock for this test."""
        lock_fd, lock_path = acquire_lock(path)
        lock_fds.append(lock_fd)
        return lock_fd, lock_path

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)
    monkeypatch.setattr(comments, "_acquire_isolated_reask_liveness_lock", _record_lock)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))
    task = asyncio.create_task(
        comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )
    )
    await asyncio.wait_for(repair_started.wait(), timeout=5.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))
    assert lock_fds
    with pytest.raises(OSError):
        comments.os.fstat(lock_fds[0])


@pytest.mark.unit
async def test_isolated_reask_worktree_creation_cleanup_survives_second_cancellation(
    tmp_path: Path,
) -> None:
    """A second shutdown cancel cannot strand a checkout created before cancellation."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_second_cancelled")
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _CancelAfterWorktreeAddWithBlockingCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelAfterWorktreeAddWithBlockingCleanupRunner())
    )
    task = asyncio.create_task(
        comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_reports_cleanup_failure_when_creation_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cancellation cleanup is observable without swallowing cancellation."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_cancelled_cleanup_failure")
    warnings: list[tuple[str, dict[str, object]]] = []

    class _CancelAfterWorktreeAddWithFailedCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    class _RecordingLogger:
        """Test double used by the surrounding scenario."""

        def warning(self, event_name: str, **kwargs: object) -> None:
            """Capture a warning emitted by the test subject."""
            warnings.append((event_name, kwargs))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelAfterWorktreeAddWithFailedCleanupRunner())
    )
    monkeypatch.setattr(comments, "_log", _RecordingLogger())

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert warnings == [
        (
            "monitor.needs_human_reason_reask_isolated_cleanup_failed_after_creation_cancellation",
            {
                "worktree_path": str(worktree),
                "reason_code": "VALIDATION_WORKTREE_CLEANUP_FAILED",
                "message": "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout",
            },
        )
    ]


@pytest.mark.unit
async def test_isolated_reask_worktree_removal_failure_is_reported() -> None:
    """A failed isolated-checkout teardown remains a policy-blocking cleanup failure."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=1, stderr="worktree remove failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    reask_worktree = comments._IsolatedReaskWorktree(
        source_worktree=Path("/worktree"),
        path=Path("/.awf-needs-human-reask-test"),
    )

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) == (
        "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout"
    )


@pytest.mark.unit
@pytest.mark.parametrize("reask_raises", (False, True))
async def test_needs_human_reason_reask_stops_when_isolated_worktree_removal_fails(
    tmp_path: Path,
    reask_raises: bool,
) -> None:
    """A stranded isolated checkout must stop later review-repair items."""
    workspace_id = "ws_reask_remove_failure"
    worktree = _init_real_worktree(tmp_path, workspace_id)

    class _FailingWorktreeRemoveRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="worktree remove failed",
                )
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        if reask_raises:
            raise RuntimeError("re-ask failed")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        return

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_FailingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(
        _MonitorPolicyBlockedError,
        match="git worktree remove.*could not remove",
    ) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "cleanup_error",
    (
        GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr="worktree remove failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        ),
        OSError("worktree remove unavailable"),
        RuntimeError("worktree remove failed"),
    ),
)
@pytest.mark.parametrize("reask_raises", (False, True))
async def test_needs_human_reason_reask_stops_when_isolated_worktree_removal_raises(
    tmp_path: Path,
    cleanup_error: Exception,
    reask_raises: bool,
) -> None:
    """An isolated-removal exception must block later review-repair items."""
    workspace_id = "ws_reask_remove_exception"
    worktree = _init_real_worktree(tmp_path, workspace_id)

    class _ExceptionalWorktreeRemoveRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str]) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                raise cleanup_error
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        if reask_raises:
            raise RuntimeError("re-ask failed")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        pytest.fail("a stranded isolated checkout must block the fix cycle")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_ExceptionalWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    (
        _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
        _MonitorAgentServiceRecoveryFailedError(
            "clarification model service recovery failed",
            reason_code="CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED",
        ),
        _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING_UNRECOVERABLE"),
        _MonitorMirrorHooksPathRepairFailedError(),
        _MonitorPolicyBlockedError("policy blocked"),
    ),
)
@pytest.mark.parametrize("cleanup_fails", (False, True))
async def test_needs_human_reason_reask_reraises_terminal_repair_errors(
    error: Exception,
    cleanup_fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal repair failures must reach the fix-cycle reason-code handlers."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        raise error

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        pytest.fail("terminal re-ask error must not be replaced with a missing reason")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "a" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str | None:
        """Assert the primary worktree stays unchanged in this test."""
        cleanup_calls.append(kwargs)
        if cleanup_fails:
            return "could not inspect primary worktree"
        return None

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    with pytest.raises(type(error)) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id="ws_1",
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head="a" * 40,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value is error
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "a" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_records_clarification_unavailable_for_hosted_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted re-asks remain skipped and report why no re-ask was attempted."""
    invoked = False
    cleanup_called = False
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _check_reask_primary_worktree_clean(_runner: object, **_kwargs: object) -> None:
        """Assert the primary worktree stays unchanged in this test."""
        nonlocal cleanup_called
        cleanup_called = True

    runner = SimpleNamespace(
        _deps=SimpleNamespace(adapter=SimpleNamespace(is_hosted=True)),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id="ws_1",
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=Path("compose.yml"),
        state=None,
        task_tag=None,
        operation_start_head="a" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert cleanup_called is False
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_needs_human_reason_reask_skips_when_primary_worktree_loses_git_control_file(
    tmp_path: Path,
) -> None:
    """A real workspace without Git metadata never falls back to an unisolated run."""
    invoked = False
    audit_events: list[dict[str, object]] = []
    workspace_id = "ws_reask_missing_git_control_file"
    (tmp_path / workspace_id).mkdir()

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="must not be used")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        pytest.fail("missing Git metadata must skip the clarification re-ask")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=Path("compose.yml"),
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_needs_human_reason_reask_skips_when_production_worktree_is_missing(
    tmp_path: Path,
) -> None:
    """A missing production workspace never falls back to an unisolated run."""
    invoked = False
    audit_events: list[dict[str, object]] = []
    workspace_id = "ws_reask_missing_worktree"

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="must not be used")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        pytest.fail("a missing production worktree must skip the clarification re-ask")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=Path("compose.yml"),
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_needs_human_reason_reask_does_not_commit_dirty_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clarification re-ask must discard edits instead of committing them."""
    committed_messages: list[str] = []
    cleanup_calls: list[dict[str, object]] = []

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        """Exercise the _provider_recovery_suppresses_cli test helper."""
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        """Exercise the _run_monitor_agent_with_service_recovery test helper."""
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: select the deployment region",
            stderr="",
        )

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        """Exercise the _commit_dirty_worktree test helper."""
        committed_messages.append(str(kwargs["message"]))
        return True

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        return

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "b" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> None:
        """Assert the primary worktree stays unchanged in this test."""
        cleanup_calls.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    (tmp_path / "ws_1").mkdir()

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        return await comments._invoke_cli_for_verdict_result(runner, **kwargs)

    runner._invoke_cli_for_verdict_result = _invoke_cli_for_verdict_result
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id="ws_1",
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=Path("compose.yml"),
        state=None,
        task_tag=None,
        operation_start_head="b" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert committed_messages == []
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "b" * 40,
        }
    ]
