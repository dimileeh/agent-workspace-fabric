"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

import asyncio
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
    _MonitorPolicyBlockedError,
)
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
    """Create a committed worktree suitable for direct re-ask helper coverage."""
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


def _init_awf_linked_worktree(tmp_path: Path, workspace_id: str) -> Path:
    """Create a valid AWF-shaped linked worktree for monitor entrypoint coverage."""
    source = tmp_path / f"{workspace_id}-source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "awf@example.com")
    _git(source, "config", "user.name", "AWF Test")
    (source / ".gitignore").write_text("*.env\n", encoding="utf-8")
    (source / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(source, "add", ".gitignore", "tracked.py")
    _git(source, "commit", "-qm", "initial")

    worktree = tmp_path / workspace_id
    mirror = tmp_path.parent / "mirrors" / f"{tmp_path.name}-{workspace_id}.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(mirror)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(mirror), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    return worktree


class _LocalCommandRunner:
    """Run the PR monitor's git commands against a temporary real worktree."""

    async def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run this test double and record the invocation."""
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_seconds, env=env
        )
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
    source_mirror = tmp_path / "source-mirror"
    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))
    result = await agent_service_recovery._run_monitor_agent_with_service_recovery(
        runner,
        workspace_id="ws_reask",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        prompt="state the reason",
        log_source="recovery",
        isolated_worktree_host_path=reask_worktree,
        isolated_worktree_ref="a" * 40,
        isolated_worktree_source_mirror=source_mirror,
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
            "isolated_worktree_ref": "a" * 40,
            "isolated_worktree_source_mirror": source_mirror,
        }
    ]


@pytest.mark.unit
async def test_isolated_reask_ref_passes_from_comment_verdict_to_service_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trusted re-ask ref reaches the service-recovery adapter call."""
    calls: list[dict[str, object]] = []

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**kwargs: object) -> AgentRunResult:
        calls.append(dict(kwargs))
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: reason",
            stderr="",
        )

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    workspace_id = "ws_reask"
    (tmp_path / workspace_id).mkdir()
    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
    )
    monkeypatch.setattr(
        comments,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _worktree_path: None)

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="state the reason",
        commit_message="fix: address thread_1",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        commit_dirty_changes=False,
        isolated_worktree_host_path=tmp_path / ".awf-needs-human-reask-test",
        isolated_worktree_ref="a" * 40,
        isolated_worktree_source_mirror=tmp_path / "source-mirror",
    )

    assert result == VerdictResult(verdict="needs_human", reason="reason")
    assert calls[0]["isolated_worktree_ref"] == "a" * 40
    assert calls[0]["isolated_worktree_source_mirror"] == tmp_path / "source-mirror"


@pytest.mark.unit
@pytest.mark.parametrize("isolated", (False, True))
async def test_verdict_repairs_primary_worktree_only_for_nonisolated_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated: bool,
) -> None:
    """Only ordinary comment repair runs mutate the primary worktree and mirror."""
    primary_repairs: list[dict[str, object]] = []
    mirror_repairs: list[Path] = []

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: reason",
            stderr="",
        )

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        primary_repairs.append(dict(kwargs))
        return True

    async def _repair_mirror_hooks_path(mirror_path: Path) -> None:
        mirror_repairs.append(mirror_path)

    workspace_id = "ws_reask"
    primary_worktree = tmp_path / workspace_id
    primary_worktree.mkdir()
    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
    )
    monkeypatch.setattr(
        comments,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        comments,
        "mirror_path_for_worktree",
        lambda _worktree_path: tmp_path / "mirror.git",
    )
    monkeypatch.setattr(comments, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    invocation_kwargs: dict[str, object] = {}
    if isolated:
        invocation_kwargs = {
            "isolated_worktree_host_path": tmp_path / ".awf-needs-human-reask-test",
            "isolated_worktree_ref": "a" * 40,
        }

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="state the reason",
        commit_message="fix: address thread_1",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        commit_dirty_changes=False,
        **invocation_kwargs,
    )

    assert result == VerdictResult(verdict="needs_human", reason="reason")
    if isolated:
        assert primary_repairs == []
        assert mirror_repairs == []
    else:
        assert len(primary_repairs) == 1
        assert primary_repairs[0]["worktree_path"] == primary_worktree
        assert mirror_repairs == [tmp_path / "mirror.git"]


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
async def test_read_only_hosted_reask_does_not_retry_or_sync_the_pr_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted clarification error is advisory and cannot trigger a PR-head sync."""
    calls: list[dict[str, object]] = []

    class _Adapter:
        """Test double used by the surrounding scenario."""

        is_hosted = True
        name = AgentRuntime.codex

        async def run(self, **kwargs: object) -> AgentRunResult:
            """Record the host contract then return a failed clarification."""
            calls.append(dict(kwargs))
            raise AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(returncode=1, stdout="", stderr="timed out"),
                reason_code="AGENT_TIMEOUT",
                details={"terminal_head_sha": "b" * 40},
            )

    async def _load_workspace(_workspace_id: str) -> SimpleNamespace:
        """Provide the trusted hosted PR identity for this test."""
        return SimpleNamespace(
            repo_url="git@github.com:dimileeh/aira-web.git",
            pr_url="https://github.com/dimileeh/aira-web/pull/1",
            pr_number=1,
            branch_base="main",
            remote_push_branch="awf/ws_reask",
            owned_paths=[],
            monitor_last_commit_sha="a" * 40,
            task_policy={},
        )

    async def _unexpected_sync(**_kwargs: object) -> str:
        """Fail if a read-only run is treated as a normal hosted repair."""
        raise AssertionError("read-only clarification must not sync a terminal PR head")

    monkeypatch.setattr(
        agent_service_recovery,
        "_sync_hosted_worktree_to_terminal_head",
        _unexpected_sync,
    )
    runner = SimpleNamespace(
        _deps=SimpleNamespace(adapter=_Adapter()),
        _load_workspace=_load_workspace,
        _worktrees_root=tmp_path,
    )

    with pytest.raises(AgentRunError, match="timed out"):
        await agent_service_recovery._run_monitor_agent_with_service_recovery(
            runner,
            workspace_id="ws_reask",
            compose_project="awf_ws_reask",
            compose_file=tmp_path / "compose.yml",
            prompt="state the reason",
            log_source="recovery",
            read_only=True,
        )

    assert len(calls) == 1
    assert calls[0]["read_only"] is True
    assert calls[0]["worktree_path"] == tmp_path / "ws_reask"


@pytest.mark.unit
async def test_read_only_hosted_reask_accepts_a_reason_without_terminal_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safe hosted clarification never needs the normal repair-head proof."""
    calls: list[dict[str, object]] = []

    class _Adapter:
        """Test double used by the surrounding scenario."""

        is_hosted = True
        name = AgentRuntime.codex

        async def run(self, **kwargs: object) -> AgentRunResult:
            """Return a reason-only answer with no PR head advancement."""
            calls.append(dict(kwargs))
            return AgentRunResult(
                returncode=0,
                stdout="AWF-VERDICT: NEEDS_HUMAN: select the deployment region",
                stderr="",
            )

    async def _load_workspace(_workspace_id: str) -> SimpleNamespace:
        """Provide the trusted hosted PR identity for this test."""
        return SimpleNamespace(
            repo_url="git@github.com:dimileeh/aira-web.git",
            pr_url="https://github.com/dimileeh/aira-web/pull/1",
            pr_number=1,
            branch_base="main",
            remote_push_branch="awf/ws_reask",
            owned_paths=[],
            monitor_last_commit_sha="a" * 40,
            task_policy={},
        )

    async def _unexpected_sync(**_kwargs: object) -> str:
        """Fail if a read-only run is treated as a normal hosted repair."""
        raise AssertionError("read-only clarification must not sync a terminal PR head")

    monkeypatch.setattr(
        agent_service_recovery,
        "_sync_hosted_worktree_to_terminal_head",
        _unexpected_sync,
    )
    runner = SimpleNamespace(
        _deps=SimpleNamespace(adapter=_Adapter()),
        _load_workspace=_load_workspace,
        _worktrees_root=tmp_path,
    )

    result = await agent_service_recovery._run_monitor_agent_with_service_recovery(
        runner,
        workspace_id="ws_reask",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        prompt="state the reason",
        log_source="recovery",
        read_only=True,
    )

    assert result.stdout.endswith("select the deployment region")
    assert calls[0]["read_only"] is True


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
            "repair_shared_git_metadata": False,
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
async def test_isolated_reask_worktree_bounds_creation_metadata_lookup_and_checkout_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An included special file cannot block re-ask worktree setup forever."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_creation_timeout")

    class _RecordingLocalCommandRunner(_LocalCommandRunner):
        """Capture timeouts used while setting up the linked worktree."""

        def __init__(self) -> None:
            self.creation_timeouts: list[float | None] = []
            self.filter_metadata_timeouts: list[float | None] = []
            self.checkout_timeouts: list[float | None] = []

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Record the requested timeout and run the real Git command."""
            if "worktree" in args and "add" in args:
                self.creation_timeouts.append(timeout_seconds)
            if "ls-tree" in args:
                self.filter_metadata_timeouts.append(timeout_seconds)
            if "checkout" in args:
                self.checkout_timeouts.append(timeout_seconds)
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    command_runner = _RecordingLocalCommandRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Avoid changing ownership while exercising the Git invocation."""
        return True

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
    )

    assert command_runner.creation_timeouts == [
        comments._ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS
    ]
    assert command_runner.filter_metadata_timeouts == [
        comments._ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS
    ]
    assert command_runner.checkout_timeouts == [
        comments._ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS
    ]
    assert reask_worktree is not None
    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None


@pytest.mark.unit
async def test_isolated_reask_worktree_disables_primary_fsmonitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-ask checkout cannot run a monitor command from the prior agent."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_fsmonitor_disabled")
    fsmonitor_marker = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "fsmonitor"
    fsmonitor.write_text(
        f"#!/bin/sh\\ntouch '{fsmonitor_marker}'\\nprintf 'token\\n'\\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o755)
    _git(worktree, "config", "core.fsmonitor", str(fsmonitor))

    class _RecordingLocalCommandRunner(_LocalCommandRunner):
        """Record Git invocations while exercising the real checkout path."""

        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.primary_status_timeouts: list[float | None] = []

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Record and run a Git command against the temporary worktree."""
            self.commands.append(args)
            if args[args.index("-C") + 1] == str(worktree) and "status" in args:
                self.primary_status_timeouts.append(timeout_seconds)
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    command_runner = _RecordingLocalCommandRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    restore_ref = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the unchanged primary-worktree revision."""
        return restore_ref

    runner._rev_parse_head = _rev_parse_head

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Avoid changing ownership while exercising the Git invocation."""
        return True

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert reask_worktree is not None
    assert (
        await comments._check_reask_primary_worktree_clean(
            runner,
            worktree_path=worktree,
            restore_ref=restore_ref,
        )
        is None
    )
    assert not fsmonitor_marker.exists()
    primary_status_commands = [
        args
        for args in command_runner.commands
        if args[args.index("-C") + 1] == str(worktree) and "status" in args
    ]
    assert len(primary_status_commands) == 2
    assert all("core.fsmonitor=false" in args for args in primary_status_commands)
    assert command_runner.primary_status_timeouts == [
        comments._ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS,
        comments._ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS,
    ]
    checkout_commands = [args for args in command_runner.commands if "checkout" in args]
    assert len(checkout_commands) == 1
    assert "core.fsmonitor=false" in checkout_commands[0]
    assert "--no-recurse-submodules" in checkout_commands[0]
    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None


@pytest.mark.unit
async def test_isolated_reask_worktree_disables_primary_checkout_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-ask checkout cannot run a filter configured by the previous agent."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_filters_disabled")
    (worktree / ".gitattributes").write_text(
        "# Driver names must come from the pinned commit.\nfiltered.txt filter=poison\n",
        encoding="utf-8",
    )
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
async def test_isolated_reask_worktree_disables_filter_configured_after_metadata_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter configured after metadata discovery cannot run during checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_filter_config_toctou")
    (worktree / ".gitattributes").write_text("filtered.txt filter=evil\n", encoding="utf-8")
    (worktree / "filtered.txt").write_text("original content\n", encoding="utf-8")
    _git(worktree, "add", ".gitattributes", "filtered.txt")
    _git(worktree, "commit", "-qm", "add filtered file")
    filter_marker = tmp_path / "late-smudge-filter-ran"

    class _FilterConfigurationMutationRunner(_LocalCommandRunner):
        """Configure the filter after metadata discovery and before checkout."""

        filter_configured = False

        def _configure_filter(self) -> None:
            """Simulate a prior agent adding an executable filter driver."""
            if self.filter_configured:
                return
            self.filter_configured = True
            _git(worktree, "config", "filter.evil.smudge", f"touch '{filter_marker}'; cat")
            _git(worktree, "config", "filter.evil.clean", "cat")
            _git(worktree, "config", "filter.evil.required", "true")

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run metadata lookup, then simulate a prior agent mutating mirror config."""
            if "checkout" in args:
                self._configure_filter()
                return await super().run(args, timeout_seconds=timeout_seconds, env=env)
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if "ls-tree" in args:
                self._configure_filter()
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_FilterConfigurationMutationRunner()))

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
    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None


@pytest.mark.unit
async def test_isolated_reask_worktree_disables_filters_from_linked_worktree_include(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conditional filter for the linked gitdir cannot run while it is populated."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_conditional_filters_disabled")
    (worktree / ".gitattributes").write_text("filtered.txt filter=poison\n", encoding="utf-8")
    (worktree / "filtered.txt").write_text("original content\n", encoding="utf-8")
    _git(worktree, "add", ".gitattributes", "filtered.txt")
    _git(worktree, "commit", "-qm", "add filtered file")
    filter_marker = tmp_path / "conditional-smudge-filter-ran"
    included_config = tmp_path / "linked-worktree-filter.conf"
    included_config.write_text(
        '[filter "poison"]\n'
        f"\tsmudge = touch '{filter_marker}'; cat\n"
        "\tclean = cat\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    _git(
        worktree,
        "config",
        "--add",
        "includeIf.gitdir:**/worktrees/*isolated_reask_*.path",
        str(included_config),
    )
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
async def test_isolated_reask_worktree_does_not_read_filter_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkout does not need to inspect mutable filter configuration."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_configuration_free_checkout")

    class _FailFilterConfigurationReadRunner(_LocalCommandRunner):
        """Reject any attempt to inspect the source checkout's filter config."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run the checkout but fail if it reads filter configuration."""
            if "--get-regexp" in args:
                return CommandResult(returncode=2, stdout="", stderr="config unreadable")
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_FailFilterConfigurationReadRunner()))

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
async def test_isolated_reask_worktree_blocks_when_checkout_cleanup_fails(
    tmp_path: Path,
) -> None:
    """A checkout failure remains blocking when its cleanup cannot be confirmed."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_checkout_cleanup_failure")

    class _FailedCheckoutWithFailedCleanupRunner(_LocalCommandRunner):
        """Fail checkout after worktree registration, then its removal."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run real setup commands except for the synthetic failure points."""
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            if "checkout" in args:
                return CommandResult(returncode=2, stdout="", stderr="checkout failed")
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_FailedCheckoutWithFailedCleanupRunner()))

    with pytest.raises(comments._IsolatedReaskWorktreeCleanupFailedError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))


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
    command_runner.queue_result(returncode=1, stderr="worktree add failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not create an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert "worktree" in command_runner.calls[1].args
    assert "add" in command_runner.calls[1].args
    assert "--no-checkout" in command_runner.calls[1].args


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_after_nonzero_creation_result(
    tmp_path: Path,
) -> None:
    """A failed worktree-add result cannot leave its populated checkout behind."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_nonzero_after_checkout")

    class _NonzeroAfterWorktreeAddRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
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
@pytest.mark.parametrize("failure_stage", ("creation", "population"))
async def test_needs_human_reason_reask_blocks_when_setup_cleanup_fails(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    """A failed setup cleanup cannot be treated as an advisory re-ask failure."""
    workspace_id = "ws_reask_create_cleanup_failure"
    worktree = _init_awf_linked_worktree(tmp_path, workspace_id)
    audit_events: list[dict[str, object]] = []

    class _NonzeroSetupWithFailedCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if (failure_stage == "creation" and "worktree" in args and "add" in args) or (
                failure_stage == "population" and "checkout" in args
            ):
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr=f"{failure_stage} failed",
                )
            return result

    async def _rev_parse_head(_worktree_path: Path, *, timeout_seconds: float | None = None) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_NonzeroSetupWithFailedCleanupRunner()),
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
@pytest.mark.parametrize("failure_stage", ("creation", "population"))
async def test_needs_human_reason_reask_blocks_when_setup_cleanup_raises(
    tmp_path: Path,
    cleanup_error: Exception,
    failure_stage: str,
) -> None:
    """A failed setup-removal exception cannot become an advisory re-ask failure."""
    workspace_id = "ws_reask_create_cleanup_exception"
    worktree = _init_awf_linked_worktree(tmp_path, workspace_id)
    audit_events: list[dict[str, object]] = []

    class _NonzeroSetupWithExceptionalCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                raise cleanup_error
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if (failure_stage == "creation" and "worktree" in args and "add" in args) or (
                failure_stage == "population" and "checkout" in args
            ):
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr=f"{failure_stage} failed",
                )
            return result

    async def _rev_parse_head(_worktree_path: Path, *, timeout_seconds: float | None = None) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_NonzeroSetupWithExceptionalCleanupRunner()),
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

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
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
async def test_isolated_reask_worktree_removes_checkout_when_filter_metadata_lookup_is_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation during pinned filter discovery cannot strand the checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_filter_metadata_lookup_cancelled")

    class _CancelDuringFilterMetadataLookupRunner(_LocalCommandRunner):
        """Cancel after the linked worktree is registered and its tree is read."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run setup until pinned filter metadata is queried."""
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if "ls-tree" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelDuringFilterMetadataLookupRunner())
    )

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

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
                cleanup_finished.set()
                return result
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
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

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
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
