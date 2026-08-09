"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import comments, pre_push_validation
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
async def test_ignored_reask_snapshot_normalizes_agent_scratch_and_empty_directories(
    tmp_path: Path,
) -> None:
    """A benign post-repair tree must not block the clarification re-ask."""
    worktree = _init_real_worktree(tmp_path, "ws_snapshot_normalization")
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

    snapshot = await comments._snapshot_reask_ignored_paths(
        runner,
        worktree_path=worktree,
    )

    assert snapshot is not None
    assert snapshot.paths == (".agent-scratch/",)
    assert not empty_output_dir.exists()
    assert (
        await comments._restore_reask_ignored_paths(
            runner,
            worktree_path=worktree,
            snapshot=snapshot,
        )
        is None
    )
    assert scratch.read_text(encoding="utf-8") == "runtime state\n"


@pytest.mark.unit
async def test_ignored_reask_restore_failure_retains_snapshot_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed restore must leave the only original ignored-file copy recoverable."""
    worktree = _init_real_worktree(tmp_path, "ws_restore_failure")
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))
    snapshot = await comments._snapshot_reask_ignored_paths(
        runner,
        worktree_path=worktree,
    )
    assert snapshot is not None

    def _copy_failure(_source: Path, _destination: Path) -> None:
        raise OSError("destination is unwritable")

    monkeypatch.setattr(comments, "_copy_ignored_snapshot_entry", _copy_failure)
    try:
        failure = await comments._restore_reask_ignored_paths(
            runner,
            worktree_path=worktree,
            snapshot=snapshot,
        )

        assert failure is not None
        assert str(snapshot.root) in failure
        assert snapshot.root.exists()
        assert not config.exists()
    finally:
        shutil.rmtree(snapshot.root, ignore_errors=True)


@pytest.mark.unit
def test_ignored_reask_snapshot_helpers_preserve_paths_without_following_links(
    tmp_path: Path,
) -> None:
    """The ignored-file backup must preserve safe content and reject path escapes."""
    source_root = tmp_path / "source"
    snapshot_root = tmp_path / "snapshot"
    source_root.mkdir()
    nested = source_root / "ignored-dir"
    nested.mkdir()
    (nested / "config.env").write_text("MODE=original\n", encoding="utf-8")
    comments._copy_ignored_snapshot_entry(nested, snapshot_root / nested.name)
    assert (snapshot_root / nested.name / "config.env").read_text(encoding="utf-8") == (
        "MODE=original\n"
    )
    comments._remove_ignored_snapshot_entry(snapshot_root / nested.name)
    assert not (snapshot_root / nested.name).exists()

    outside = tmp_path / "outside.env"
    outside.write_text("outside\n", encoding="utf-8")
    link = source_root / "ignored-link"
    link.symlink_to(outside)
    copied_link = snapshot_root / link.name
    comments._copy_ignored_snapshot_entry(link, copied_link)
    assert copied_link.is_symlink()
    assert copied_link.readlink() == outside
    comments._remove_ignored_snapshot_entry(copied_link)
    assert not copied_link.exists()
    assert outside.exists()

    fifo = source_root / "ignored-fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="unsupported ignored path type"):
        comments._copy_ignored_snapshot_entry(fifo, snapshot_root / fifo.name)
    with pytest.raises(ValueError, match="unsafe ignored worktree path"):
        comments._snapshot_relative_path("../escape.env")
    assert comments._collapsed_snapshot_paths(("ignored-dir/child.env", "ignored-dir/")) == (
        "ignored-dir/",
    )

    symlink_parent = source_root / "linked-parent"
    symlink_parent.symlink_to(tmp_path)
    with pytest.raises(OSError, match="restore parent is a symlink"):
        comments._safe_restore_destination(
            source_root,
            comments._snapshot_relative_path("linked-parent/config.env"),
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
async def test_needs_human_reason_reask_restores_ignored_files_before_continuing(
    tmp_path: Path,
) -> None:
    """A clarification re-ask must not leak ignored config changes into the next repair."""
    workspace_id = "ws_ignored_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")
    generated = worktree / "generated.env"

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        config.write_text("MODE=clarification-edit\n", encoding="utf-8")
        generated.write_text("GENERATED=during-reask\n", encoding="utf-8")
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
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert not generated.exists()


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
async def test_needs_human_reason_reask_blocks_when_dirty_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed re-ask cleanup must stop the cycle before another item can commit it."""
    cleanup_calls: list[dict[str, object]] = []

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

    with pytest.raises(_MonitorPolicyBlockedError, match="could not remove re-ask edits") as raised:
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

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "c" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_blocks_when_cleanup_fails_after_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cleanup after an error must stop the next fix-cycle item."""
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

    with pytest.raises(_MonitorPolicyBlockedError, match="could not remove re-ask edits") as raised:
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
            operation_start_head="d" * 40,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "re-ask failed"
    assert audit_events == []


@pytest.mark.unit
async def test_needs_human_reason_reask_requires_a_restore_ref(
    tmp_path: Path,
) -> None:
    """Do not run the re-ask when its non-mutating cleanup cannot be anchored."""
    invoked = False

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="select a region")

    async def _rev_parse_head(_worktree_path: Path) -> None:
        return None

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    with pytest.raises(
        _MonitorPolicyBlockedError, match="Could not capture a worktree restore ref"
    ):
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

    assert invoked is False


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
