"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import agent_service_recovery, comments, pre_push_validation
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import _sanitize_verdict_reason
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
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
        is_hosted = False

        async def run(self, **kwargs: object) -> AgentRunResult:
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
async def test_isolated_reask_worktree_excludes_preexisting_ignored_dependencies(
    tmp_path: Path,
) -> None:
    """The clarification checkout contains tracked source, not ignored dependency trees."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_isolation")
    (worktree / ".gitignore").write_text("*.env\n.venv/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore dependencies")
    dependency = worktree / ".venv" / "lib" / "dependency.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("large dependency tree\n", encoding="utf-8")
    scratch = worktree / ".agent-scratch" / "session.txt"
    scratch.parent.mkdir()
    scratch.write_text("runtime state\n", encoding="utf-8")
    empty_output_dir = worktree / "removed-output"
    empty_output_dir.mkdir()
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=_LocalCommandRunner(),
            adapter=SimpleNamespace(runtime_scratch_paths=(".agent-scratch/",)),
        )
    )

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
    )

    assert reask_worktree is not None
    assert reask_worktree.path.parent == worktree
    assert (reask_worktree.path / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (reask_worktree.path / ".venv").exists()
    assert not (reask_worktree.path / ".agent-scratch").exists()
    assert dependency.exists()
    assert scratch.exists()
    assert not empty_output_dir.exists()

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None
    assert not reask_worktree.path.exists()


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


@pytest.mark.unit
async def test_isolated_reask_worktree_removal_failure_is_reported() -> None:
    """A failed isolated-checkout teardown remains a policy-blocking cleanup failure."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=1, stderr="worktree remove failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    reask_worktree = comments._IsolatedReaskWorktree(
        source_worktree=Path("/worktree"),
        path=Path("/worktree/.awf-needs-human-reask-test"),
    )

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) == (
        "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    (
        _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
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
        raise error

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        pytest.fail("terminal re-ask error must not be replaced with a missing reason")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "a" * 40

    async def _cleanup_reask_worktree(_runner: object, **kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(kwargs)
        if cleanup_fails:
            return SimpleNamespace(
                ok=False,
                reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
                message="could not remove re-ask edits",
            )
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
        nonlocal invoked
        invoked = True
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal cleanup_called
        cleanup_called = True
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(adapter=SimpleNamespace(is_hosted=True)),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="must not be used")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
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
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_MISSING"


@pytest.mark.unit
async def test_needs_human_reason_reask_does_not_commit_dirty_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clarification re-ask must discard edits instead of committing them."""
    committed_messages: list[str] = []
    cleanup_calls: list[dict[str, object]] = []

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: select the deployment region",
            stderr="",
        )

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        committed_messages.append(str(kwargs["message"]))
        return True

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "b" * 40

    async def _cleanup_reask_worktree(_runner: object, **kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(kwargs)
        return SimpleNamespace(ok=True)

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
        return await comments._invoke_cli_for_verdict_result(runner, **kwargs)

    runner._invoke_cli_for_verdict_result = _invoke_cli_for_verdict_result
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_post_repair_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification cleanup must not reset the repair commit that preceded it."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "b" * 40

    async def _cleanup_reask_worktree(_runner: object, **kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(kwargs)
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "b" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_isolates_ignored_files_before_continuing(
    tmp_path: Path,
) -> None:
    """A clarification re-ask must not see or alter ignored primary-worktree files."""
    workspace_id = "ws_ignored_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")
    dependency = worktree / ".venv" / "dependency.py"
    dependency.parent.mkdir()
    dependency.write_text("dependency\n", encoding="utf-8")
    (worktree / ".gitignore").write_text("*.env\n.venv/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore dependencies")
    reask_worktree_paths: list[Path] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        reask_worktree_paths.append(reask)
        assert not (reask / ".venv").exists()
        (reask / ".env").write_text("MODE=clarification-edit\n", encoding="utf-8")
        (reask / "generated.env").write_text("GENERATED=during-reask\n", encoding="utf-8")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
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

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert reask_worktree_paths[0].parent == worktree
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert dependency.exists()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_cleans_worktree_when_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation must not leave clarification edits for the next fix-cycle item."""
    workspace_id = "ws_cancelled_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        (reask / ".env").write_text("MODE=clarification-edit\n", encoding="utf-8")
        (reask / "generated.env").write_text("GENERATED=during-reask\n", encoding="utf-8")
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    with pytest.raises(asyncio.CancelledError):
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

    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_cleanup_survives_second_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second shutdown cancel cannot strand the isolated clarification checkout."""
    workspace_id = "ws_reask_second_cancel"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _BlockingWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
    )

    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
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
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_reraises_cancellation_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not replace the monitor's cancellation signal."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "e" * 40

    async def _cleanup_reask_worktree(_runner: object, **kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(kwargs)
        return SimpleNamespace(
            ok=False,
            reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
            message="could not remove re-ask edits",
        )

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
    )

    with pytest.raises(asyncio.CancelledError):
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
            operation_start_head=None,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "e" * 40,
        }
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "credential_only_reason",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890.",
        '"ghp_abcdefghijklmnopqrstuvwxyz1234567890"',
    ),
)
def test_sanitize_verdict_reason_treats_credential_only_reason_as_missing(
    credential_only_reason: str,
) -> None:
    """A redacted credential alone is not an actionable operator decision."""
    assert _sanitize_verdict_reason(credential_only_reason) is None


@pytest.mark.unit
def test_sanitize_verdict_reason_preserves_meaningful_text_with_redacted_details() -> None:
    reason = "A maintainer must decide whether to rotate GITHUB_TOKEN=secretValue123456."

    assert _sanitize_verdict_reason(reason) == (
        "A maintainer must decide whether to rotate GITHUB_TOKEN=<redacted>"
    )


@pytest.mark.unit
async def test_needs_human_reason_reask_retains_original_verdict_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read-only cleanup must retain the original blocking verdict."""
    cleanup_calls: list[dict[str, object]] = []
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(_kwargs)
        return SimpleNamespace(
            ok=False,
            reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
            message="could not remove re-ask edits",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "c" * 40

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_MISSING"
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "c" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_retains_original_verdict_when_cleanup_fails_after_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cleanup after an error must not block the monitor."""
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise RuntimeError("re-ask failed")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "d" * 40

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            ok=False,
            reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
            message="could not remove re-ask edits",
        )

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
        operation_start_head="d" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_MISSING"


@pytest.mark.unit
async def test_needs_human_reason_reask_requires_a_restore_ref(
    tmp_path: Path,
) -> None:
    """Do not run the re-ask when its non-mutating cleanup cannot be anchored."""
    invoked = False
    audit_events: list[dict[str, object]] = []
    worktree = tmp_path / "ws_1"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: unavailable\n", encoding="utf-8")
    state = MonitorState()

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="select a region")

    async def _rev_parse_head(_worktree_path: Path) -> None:
        return None

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
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
        state=state,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert state.threads_addressed_ids == {}
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_MISSING"


@pytest.mark.unit
async def test_non_mutating_verdict_invocation_skips_commit_after_agent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed reason-only invocation must not salvage dirty agent output either."""
    committed = False

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        raise RuntimeError("failed after edit")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        nonlocal committed
        committed = True
        return True

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _commit_dirty_worktree=_commit_dirty_worktree,
    )
    (tmp_path / "ws_1").mkdir()
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)

    with pytest.raises(RuntimeError, match="failed after edit"):
        await comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_1",
            prompt="clarify the required decision",
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            commit_dirty_changes=False,
        )

    assert committed is False
