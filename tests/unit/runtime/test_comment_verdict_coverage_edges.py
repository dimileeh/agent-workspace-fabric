"""Focused branch coverage for provider-neutral comment verdict helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup


@pytest.mark.unit
async def test_owned_paths_for_prompt_or_empty_logs_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _load_failure(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(comment_verdict, "_owned_paths_for_prompt", _load_failure)
    runner = SimpleNamespace()

    assert await comment_verdict._owned_paths_for_prompt_or_empty(runner, "ws_test") == []


def _evidence_runner(
    *,
    end_head: str | None,
    descends: bool,
    trees_differ: bool,
    touches_path: bool = True,
) -> SimpleNamespace:
    async def _rev_parse_head(_path: Path) -> str | None:
        return end_head

    async def _descends(**_kwargs: object) -> bool:
        return descends

    async def _trees_differ(**_kwargs: object) -> bool:
        return trees_differ

    async def _touches_path(**_kwargs: object) -> bool:
        return touches_path

    return SimpleNamespace(
        _rev_parse_head=_rev_parse_head,
        _head_descends_from=_descends,
        _commit_trees_differ=_trees_differ,
        _commit_range_touches_path=_touches_path,
    )


@pytest.mark.unit
async def test_item_fix_evidence_checks_hosted_candidate_ancestry(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_evidence"
    worktree.mkdir()
    start = "a" * 40
    hosted = "b" * 40
    state = MonitorState(last_push_sha=hosted)
    state.hosted_terminal_head_advanced = True

    assert (
        await comment_verdict._item_fix_evidence(
            _evidence_runner(end_head=start, descends=False, trees_differ=True),
            worktree_path=worktree,
            item_start_head=start,
            item_path="src/a.py",
            item_line=3,
            state=state,
            dirty_changes_committed=False,
        )
        is False
    )


@pytest.mark.unit
async def test_item_fix_evidence_rejects_unchanged_candidate_tree(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_evidence"
    worktree.mkdir()
    start = "a" * 40
    candidate = "b" * 40

    assert (
        await comment_verdict._item_fix_evidence(
            _evidence_runner(end_head=candidate, descends=True, trees_differ=False),
            worktree_path=worktree,
            item_start_head=start,
            item_path=None,
            item_line=None,
            state=None,
            dirty_changes_committed=False,
        )
        is False
    )


@pytest.mark.unit
async def test_item_fix_evidence_rejects_missing_item_scope_helper(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_evidence"
    worktree.mkdir()
    runner = _evidence_runner(
        end_head="b" * 40,
        descends=True,
        trees_differ=True,
    )
    del runner._commit_range_touches_path

    assert (
        await comment_verdict._item_fix_evidence(
            runner,
            worktree_path=worktree,
            item_start_head="a" * 40,
            item_path="src/a.py",
            item_line=3,
            state=None,
            dirty_changes_committed=False,
        )
        is False
    )


@pytest.mark.unit
async def test_item_fix_evidence_uses_hosted_head_when_local_head_is_unreadable(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_evidence"
    worktree.mkdir()
    start = "a" * 40
    hosted = "b" * 40
    state = MonitorState(last_push_sha=hosted)
    state.hosted_terminal_head_advanced = True

    assert await comment_verdict._item_fix_evidence(
        _evidence_runner(end_head=None, descends=True, trees_differ=True),
        worktree_path=worktree,
        item_start_head=start,
        item_path=None,
        item_line=None,
        state=state,
        dirty_changes_committed=False,
    )


def _successful_cleanup(start: str) -> ValidationWorktreeCleanup:
    return ValidationWorktreeCleanup(
        cleaned=False,
        check=ValidationWorktreeCheck(clean=True),
        restore_ref=start,
    )


def _matching_head_command_runner(head: str) -> SimpleNamespace:
    """Return a command runner that proves the rollback rechecks live HEAD."""
    calls: list[list[str]] = []

    async def _run(command: list[str], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")

    return SimpleNamespace(run=_run, calls=calls)


@pytest.mark.unit
async def test_hosted_rollback_disables_remote_rewind_when_candidate_is_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "ws_rollback"
    worktree.mkdir()
    start = "a" * 40

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return _successful_cleanup(start)

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path) -> str:
        return start

    command_runner = _matching_head_command_runner(start)
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=True),
            runner=command_runner,
        ),
        _rev_parse_head=_rev_parse_head,
    )
    state = MonitorState(last_push_sha=start)
    state.hosted_terminal_head_advanced = True

    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback",
        worktree_path=worktree,
        item_start_head=start,
        item_start_last_push_sha=start,
        state=state,
    )
    assert command_runner.calls[0][-2:] == ["rev-parse", "HEAD"]


@pytest.mark.unit
async def test_hosted_rollback_skips_remote_rewind_without_an_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "ws_rollback"
    worktree.mkdir()
    start = "a" * 40

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return _successful_cleanup(start)

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path) -> str:
        return start

    command_runner = _matching_head_command_runner(start)
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=True),
            runner=command_runner,
        ),
        _rev_parse_head=_rev_parse_head,
    )
    state = MonitorState(last_push_sha=start)

    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback",
        worktree_path=worktree,
        item_start_head=start,
        item_start_last_push_sha=start,
        state=state,
    )
    assert command_runner.calls[0][-2:] == ["rev-parse", "HEAD"]


@pytest.mark.unit
async def test_hosted_rollback_fails_when_remote_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "ws_rollback"
    worktree.mkdir()
    start = "a" * 40
    published = "b" * 40

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return _successful_cleanup(start)

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path) -> str:
        return start

    command_runner = _matching_head_command_runner(start)
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=True),
            runner=command_runner,
        ),
        _rev_parse_head=_rev_parse_head,
    )
    state = MonitorState(last_push_sha=published)
    state.hosted_terminal_head_advanced = True

    assert (
        await comment_verdict._rollback_unaccepted_protocol_retry_changes(
            runner,
            workspace_id="ws_rollback",
            worktree_path=worktree,
            item_start_head=start,
            item_start_last_push_sha=start,
            state=state,
        )
        is False
    )
    assert command_runner.calls[0][-2:] == ["rev-parse", "HEAD"]


@pytest.mark.unit
def test_correction_authored_mutation_vs_start_detects_head_and_residue() -> None:
    assert (
        comment_verdict._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="b" * 40,
            correction_start_residue_fp="",
            pre_sink_residue_fp="",
        )
        is True
    )
    assert (
        comment_verdict._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp="",
            pre_sink_residue_fp="src/x.py",
        )
        is True
    )
    assert (
        comment_verdict._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp="src/x.py",
            pre_sink_residue_fp="src/x.py",
        )
        is False
    )
    assert (
        comment_verdict._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp=None,
            pre_sink_residue_fp="src/x.py",
        )
        is True
    )
    assert (
        comment_verdict._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp="",
            pre_sink_residue_fp=None,
        )
        is True
    )


@pytest.mark.unit
def test_stranded_residue_is_correction_mutation_attributes_preexisting() -> None:
    assert (
        comment_verdict._stranded_residue_is_correction_mutation(
            correction_start_residue_fp="src/x.py",
            post_residue_fp="src/x.py",
        )
        is False
    )
    assert (
        comment_verdict._stranded_residue_is_correction_mutation(
            correction_start_residue_fp="",
            post_residue_fp="src/x.py",
        )
        is True
    )
    assert (
        comment_verdict._stranded_residue_is_correction_mutation(
            correction_start_residue_fp="src/x.py",
            post_residue_fp=None,
        )
        is True
    )
    assert (
        comment_verdict._stranded_residue_is_correction_mutation(
            correction_start_residue_fp=None,
            post_residue_fp="src/x.py",
        )
        is True
    )


@pytest.mark.unit
async def test_correction_residue_probe_missing_worktree_is_clean(tmp_path: Path) -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace()))
    assert (
        await comment_verdict._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_missing",
            worktree_path=tmp_path / "missing",
        )
        is False
    )


@pytest.mark.unit
async def test_correction_residue_probe_status_failure_fails_closed(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(_cmd: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=128, stdout="", stderr="status failed")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is True
    )


@pytest.mark.unit
async def test_correction_residue_probe_spawn_failure_fails_closed(tmp_path: Path) -> None:
    """Spawn failure during residue status must fail closed like a bad status.

    Production regression for PRRT_kwDOSJAM6s6eJi5X: ``asyncio.create_subprocess_exec``
    raising ``OSError`` previously escaped the probe with no rollback at the
    correction call site.
    """
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(_cmd: list[str], **_kwargs: object) -> CommandResult:
        raise OSError("git status spawn failed")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is True
    )


@pytest.mark.unit
async def test_correction_residue_probe_clean_status_is_not_residue(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(_cmd: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is False
    )


@pytest.mark.unit
async def test_correction_residue_probe_ignores_untracked_agent_runtime(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(_cmd: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(
            returncode=0,
            stdout="?? .claude/agent-memory/reviewer/notes.md\n",
            stderr="",
        )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is False
    )


@pytest.mark.unit
async def test_correction_residue_probe_detects_pr_worthy_dirt(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(_cmd: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout=" M src/fix.py\n", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is True
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_includes_diff_content(
    tmp_path: Path,
) -> None:
    """Same dirty path with different patch bytes must not collide.

    Production regression for PRRT_kwDOSJAM6s6eKj9D: path-only fingerprints
    treated a correction edit of attempt-0 residue as pre-existing dirt.
    """
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _fingerprint_for(unstaged_diff: str, *, staged_diff: str = "") -> str | None:
        async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
            if "status" in cmd:
                return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
            if "--cached" in cmd:
                return CommandResult(returncode=0, stdout=staged_diff, stderr="")
            if "diff" in cmd:
                return CommandResult(returncode=0, stdout=unstaged_diff, stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
        return await comment_verdict._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )

    start_fp = await _fingerprint_for("diff --git a/src/x.py b/src/x.py\n-old\n")
    edited_fp = await _fingerprint_for("diff --git a/src/x.py b/src/x.py\n-old\n+new\n")
    same_again = await _fingerprint_for("diff --git a/src/x.py b/src/x.py\n-old\n")

    assert start_fp is not None and start_fp != ""
    assert edited_fp is not None and edited_fp != ""
    assert start_fp != edited_fp
    assert start_fp == same_again
    # Staging the same bytes redistributes identity across staged/unstaged hashes.
    staged_fp = await _fingerprint_for(
        "",
        staged_diff="diff --git a/src/x.py b/src/x.py\n-old\n",
    )
    assert staged_fp is not None and staged_fp != start_fp


@pytest.mark.unit
async def test_correction_residue_fingerprint_diff_failure_fails_closed(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        if "--cached" in cmd:
            return CommandResult(returncode=128, stdout="", stderr="diff failed")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_unstaged_diff_spawn_fails_closed(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        if "--cached" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="")
        if "diff" in cmd:
            raise OSError("git diff spawn failed")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_hashes_untracked_content(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()
    untracked = worktree / "src"
    untracked.mkdir()
    target = untracked / "new.py"

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="?? src/new.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    target.write_text("alpha\n", encoding="utf-8")
    first = await comment_verdict._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    target.write_text("beta\n", encoding="utf-8")
    second = await comment_verdict._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    assert first is not None and second is not None
    assert first != second
    target.unlink()
    missing = await comment_verdict._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    assert missing is not None and missing != first
