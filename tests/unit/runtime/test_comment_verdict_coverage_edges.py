"""Focused branch coverage for provider-neutral comment verdict helpers."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_residue
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup


def _init_git_worktree(worktree: Path) -> None:
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    (worktree / "src").mkdir()
    target = worktree / "src" / "x.py"
    target.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/x.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, capture_output=True)


def _init_git_worktree_with_dirty_submodule(worktree: Path, *, submodule_name: str = "sub") -> None:
    """Parent repo with a tracked submodule whose checked-out HEAD differs from the index."""
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    submodule = worktree / submodule_name
    submodule.mkdir()
    subprocess.run(["git", "init"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    (submodule / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "sub init"], cwd=submodule, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "submodule", "add", f"./{submodule_name}", submodule_name],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add sub"], cwd=worktree, check=True, capture_output=True
    )
    (submodule / "file.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "sub v2"], cwd=submodule, check=True, capture_output=True
    )


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
        comment_verdict_residue._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="b" * 40,
            correction_start_residue_fp="",
            pre_sink_residue_fp="",
        )
        is True
    )
    assert (
        comment_verdict_residue._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp="",
            pre_sink_residue_fp="src/x.py",
        )
        is True
    )
    assert (
        comment_verdict_residue._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp="src/x.py",
            pre_sink_residue_fp="src/x.py",
        )
        is False
    )
    assert (
        comment_verdict_residue._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp=None,
            pre_sink_residue_fp="src/x.py",
        )
        is True
    )
    assert (
        comment_verdict_residue._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head="a" * 40,
            correction_start_residue_fp="",
            pre_sink_residue_fp=None,
        )
        is True
    )
    assert (
        comment_verdict_residue._correction_authored_mutation_vs_start(
            attempt_start_head="a" * 40,
            pre_sink_head=None,
            correction_start_residue_fp="src/x.py",
            pre_sink_residue_fp="src/x.py",
        )
        is True
    )


@pytest.mark.unit
def test_stranded_residue_is_correction_mutation_attributes_preexisting() -> None:
    assert (
        comment_verdict_residue._stranded_residue_is_correction_mutation(
            correction_start_residue_fp="src/x.py",
            post_residue_fp="src/x.py",
        )
        is False
    )
    assert (
        comment_verdict_residue._stranded_residue_is_correction_mutation(
            correction_start_residue_fp="",
            post_residue_fp="src/x.py",
        )
        is True
    )
    assert (
        comment_verdict_residue._stranded_residue_is_correction_mutation(
            correction_start_residue_fp="src/x.py",
            post_residue_fp=None,
        )
        is True
    )
    assert (
        comment_verdict_residue._stranded_residue_is_correction_mutation(
            correction_start_residue_fp=None,
            post_residue_fp="src/x.py",
        )
        is True
    )


@pytest.mark.unit
async def test_correction_residue_probe_missing_worktree_is_clean(tmp_path: Path) -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace()))
    assert (
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
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
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
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
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
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
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
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
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
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
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
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
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    target.write_text("base\n-old\n", encoding="utf-8")
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    target.write_text("base\n-old\n+new\n", encoding="utf-8")
    edited_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    target.write_text("base\n-old\n", encoding="utf-8")
    same_again = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert edited_fp is not None and edited_fp != ""
    assert start_fp != edited_fp
    assert start_fp == same_again
    # Staging the same bytes redistributes identity across staged/unstaged hashes.
    subprocess.run(["git", "add", "src/x.py"], cwd=worktree, check=True, capture_output=True)
    staged_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    assert staged_fp is not None and staged_fp != start_fp


@pytest.mark.unit
async def test_correction_residue_fingerprint_includes_tracked_file_modes(
    tmp_path: Path,
) -> None:
    """Mode-only correction edits must not collide with attempt-0 content residue.

    Production regression for PRRT_kwDOSJAM6s6eNEe3: blob SHAs exclude file modes,
    so a correction that only flips the executable bit looked like pre-existing dirt.
    """
    worktree = tmp_path / "ws_residue_mode"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    target.write_text("base\n-edited\n", encoding="utf-8")
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue_mode",
        worktree_path=worktree,
    )
    target.chmod(0o755)
    mode_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue_mode",
        worktree_path=worktree,
    )
    target.chmod(0o644)
    same_again = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue_mode",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert mode_fp is not None and mode_fp != ""
    assert start_fp != mode_fp
    assert start_fp == same_again


@pytest.mark.unit
async def test_correction_residue_fingerprint_avoids_full_diff_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracked residue hashing must not shell out to unbounded ``git diff`` patches."""
    worktree = tmp_path / "ws_residue_bounded"
    worktree.mkdir()
    _init_git_worktree(worktree)
    (worktree / "src" / "x.py").write_text("base\n-edited\n", encoding="utf-8")

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(
        args: list[str],
        /,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        recorded.append(list(args))
        return real_run(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue.subprocess, "run", _recording_run)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    fingerprint = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue_bounded",
        worktree_path=worktree,
    )
    assert fingerprint is not None and fingerprint != ""
    for cmd in recorded:
        if "diff" not in cmd:
            continue
        assert "--name-only" in cmd
        assert "-z" in cmd


@pytest.mark.unit
async def test_correction_residue_fingerprint_diff_failure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def _fail_staged(**_kwargs: object) -> tuple[str | None, str | None]:
        return None, "deadbeef"

    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_tracked_residue_staged_and_unstaged",
        _fail_staged,
    )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_residue",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_unstaged_diff_spawn_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "ws_residue"
    worktree.mkdir()

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def _raise_on_tracked(**_kwargs: object) -> tuple[str | None, str | None]:
        raise OSError("git diff spawn failed")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_tracked_residue_staged_and_unstaged",
        _raise_on_tracked,
    )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
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
    first = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    target.write_text("beta\n", encoding="utf-8")
    second = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    assert first is not None and second is not None
    assert first != second
    target.unlink()
    missing = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_residue",
        worktree_path=worktree,
    )
    assert missing is not None and missing != first


@pytest.mark.unit
async def test_correction_residue_fingerprint_hashes_untracked_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untracked regular-file hashing must not block the asyncio event loop.

    A multi-gigabyte non-ignored artifact left by a malformed attempt would
    otherwise stall cancellation and sibling workspaces on the same loop
    (PRRT_kwDOSJAM6s6eLMRD).
    """
    worktree = tmp_path / "ws_offloop_residue"
    worktree.mkdir()
    (worktree / "src").mkdir()
    target = worktree / "src" / "artifact.bin"
    target.write_bytes(b"payload-bytes\n")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="?? src/artifact.bin\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    to_thread_funcs: list[str] = []
    original_to_thread = asyncio.to_thread

    async def _observe_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        name = getattr(func, "__name__", type(func).__name__)
        to_thread_funcs.append(str(name))
        return await original_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue.asyncio, "to_thread", _observe_to_thread)

    fingerprint = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_offloop_residue",
        worktree_path=worktree,
    )
    assert fingerprint is not None and fingerprint != ""
    assert any("untracked" in name for name in to_thread_funcs)


@pytest.mark.unit
async def test_correction_residue_fingerprint_hashes_symlink_identity_not_target(
    tmp_path: Path,
) -> None:
    """Untracked symlinks must be fingerprinted via readlink, never followed.

    Following a symlink to /dev/zero (or a huge host file) would block or
    unbounded-read the sync event-loop path before rollback (PRRT_kwDOSJAM6s6eK9AB).
    """
    worktree = tmp_path / "ws_symlink_residue"
    worktree.mkdir()
    dest_a = worktree / "target_a.txt"
    dest_b = worktree / "target_b.txt"
    dest_a.write_text("shared-payload\n", encoding="utf-8")
    dest_b.write_text("shared-payload\n", encoding="utf-8")
    (worktree / "src").mkdir()
    symlink_path = worktree / "src" / "alias"

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="?? src/alias\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    symlink_path.symlink_to(dest_a)
    dest_a_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_symlink_residue",
        worktree_path=worktree,
    )
    symlink_path.unlink()
    symlink_path.symlink_to(dest_b)
    dest_b_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_symlink_residue",
        worktree_path=worktree,
    )
    assert dest_a_fp is not None and dest_b_fp is not None
    # Same target file bytes, different link text → distinct fingerprints.
    assert dest_a_fp != dest_b_fp

    # Infinite/special targets must not be opened (would hang if followed).
    symlink_path.unlink()
    symlink_path.symlink_to("/dev/zero")
    zero_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_symlink_residue",
        worktree_path=worktree,
    )
    assert zero_fp is not None and zero_fp != ""
    assert zero_fp != dest_a_fp


@pytest.mark.unit
def test_hash_untracked_residue_paths_distinguishes_ambiguous_byte_boundaries(
    tmp_path: Path,
) -> None:
    """Per-file digests must not collide when bytes span former path delimiters.

    Production regression for PRRT_kwDOSJAM6s6eRK93: concatenating path\\0bytes\\0
    across files let (a=X, b=Y+"\\0b\\0"+Z) hash identically to (a=X+"\\0b\\0"+Y, b=Z).
    """
    worktree = tmp_path / "ws_untracked_boundary"
    worktree.mkdir()
    file_a = worktree / "a"
    file_b = worktree / "b"
    x = b"X"
    y = b"Y"
    z = b"Z"
    boundary = b"\0b\0"

    file_a.write_bytes(x)
    file_b.write_bytes(y + boundary + z)
    split_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["a", "b"],
        untracked={"a", "b"},
    )

    file_a.write_bytes(x + boundary + y)
    file_b.write_bytes(z)
    merged_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["a", "b"],
        untracked={"a", "b"},
    )

    assert split_fp is not None and merged_fp is not None
    assert split_fp != merged_fp


@pytest.mark.unit
def test_hash_untracked_residue_paths_distinguishes_regular_file_from_symlink(
    tmp_path: Path,
) -> None:
    """Regular-file bytes must not collide with symlink link-text fingerprints.

    Production regression for PRRT_kwDOSJAM6s6eRq4q: a regular file whose
    contents are ``symlink:foo`` hashed identically to a symlink pointing at
    ``foo``, letting correction residue attribution accept a non-FIXED verdict
    after rollback.
    """
    worktree = tmp_path / "ws_untracked_regular_symlink"
    worktree.mkdir()
    (worktree / "src").mkdir()
    path = "src/link"
    candidate = worktree / "src" / "link"

    candidate.write_bytes(b"symlink:foo")
    regular_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[path],
        untracked={path},
    )

    candidate.unlink()
    candidate.symlink_to("foo")
    symlink_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[path],
        untracked={path},
    )

    assert regular_fp is not None and symlink_fp is not None
    assert regular_fp != symlink_fp


@pytest.mark.unit
async def test_correction_residue_fingerprint_tracked_symlink_identity_not_target(
    tmp_path: Path,
) -> None:
    """Tracked worktree symlinks must be fingerprinted via readlink, never followed.

    ``git hash-object --path`` follows symlinks; a correction that typechanges a
    dirty tracked file to ``/dev/zero`` would hang the residue probe (5081034196).
    """
    worktree = tmp_path / "ws_tracked_symlink_residue"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    dest_a = worktree / "target_a.txt"
    dest_b = worktree / "target_b.txt"
    dest_a.write_text("shared-payload\n", encoding="utf-8")
    dest_b.write_text("shared-payload\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" T src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    target.write_text("base\n-edited\n", encoding="utf-8")
    content_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )

    target.unlink()
    target.symlink_to(dest_a)
    dest_a_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )
    target.unlink()
    target.symlink_to(dest_b)
    dest_b_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )

    assert content_fp is not None and content_fp != ""
    assert dest_a_fp is not None and dest_b_fp is not None
    assert content_fp != dest_a_fp
    assert dest_a_fp != dest_b_fp

    target.unlink()
    target.symlink_to("/dev/zero")
    zero_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )
    assert zero_fp is not None and zero_fp != ""
    assert zero_fp != dest_a_fp


@pytest.mark.unit
async def test_correction_residue_fingerprint_unreadable_tracked_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable tracked worktree blob must fail closed, not hash a collision-prone marker.

    Production regression for PRRT_kwDOSJAM6s6ePBHr: when attempt 0 leaves a dirty tracked
    file the control-plane user cannot read, ``git status`` still reports it while
    ``_git_worktree_blob_sha`` returns None; hashing ``<missing>`` collides before and
    after a correction edits the file but cannot stage it.
    """
    worktree = tmp_path / "ws_unreadable_tracked"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("base\n-edited\n", encoding="utf-8")

    real_blob_sha = comment_verdict_residue._git_worktree_blob_sha

    def _unreadable_blob(**kwargs: object) -> str | None:
        path = kwargs.get("path")
        if path == "src/x.py":
            return None
        return real_blob_sha(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_git_worktree_blob_sha", _unreadable_blob)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_tracked",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("base\n-correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_tracked",
        worktree_path=worktree,
    )
    assert correction_fp is None
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_stat_failure_not_misclassified_as_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission/stat failures must not fingerprint as tracked deletions.

    Bugbot review 5082437263: ``exists()`` returns False for ENOENT and for
    permission errors alike. When ``hash-object`` fails on an indexed but
    unreadable path, both correction probes must fail closed rather than share
    a stable ``<deleted>`` marker.
    """
    worktree = tmp_path / "ws_stat_failure_not_deletion"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("base\n-edited\n", encoding="utf-8")

    real_blob_sha = comment_verdict_residue._git_worktree_blob_sha
    real_lstat = Path.lstat

    def _unreadable_blob(**kwargs: object) -> str | None:
        path = kwargs.get("path")
        if path == "src/x.py":
            return None
        return real_blob_sha(**kwargs)  # type: ignore[arg-type]

    def _permission_denied_lstat(self: Path) -> object:
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(comment_verdict_residue, "_git_worktree_blob_sha", _unreadable_blob)
    monkeypatch.setattr(Path, "lstat", _permission_denied_lstat)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_stat_failure_not_deletion",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("base\n-correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_stat_failure_not_deletion",
        worktree_path=worktree,
    )
    assert correction_fp is None
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_tracked_deletion_is_fingerprintable(
    tmp_path: Path,
) -> None:
    """Tracked worktree deletions must fingerprint stably, not fail closed as unreadable.

    Production regression for PRRT_kwDOSJAM6s6eP-gA: ``git diff --name-only`` lists deleted
    paths while ``hash-object --path`` returns None, so identical attempt-0 delete residue
    must not poison correction attribution.
    """
    worktree = tmp_path / "ws_tracked_deletion"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.unlink()

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" D src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_deletion",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_deletion",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == repeat_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=repeat_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_dirty_gitlink_is_fingerprintable(
    tmp_path: Path,
) -> None:
    """Dirty tracked submodules must fingerprint via checked-out HEAD, not fail closed.

    Production regression for PRRT_kwDOSJAM6s6eRyfx: ``hash-object --path`` cannot hash
    gitlink directories, so identical attempt-0 submodule residue must not poison correction
    attribution when the correction makes no further change.
    """
    worktree = tmp_path / "ws_dirty_gitlink"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M sub\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_dirty_gitlink",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_dirty_gitlink",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == repeat_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=repeat_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_unreadable_untracked_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable untracked residue must fail closed, not hash a collision-prone marker.

    Production regression for PRRT_kwDOSJAM6s6eN7wf: when attempt 0 leaves a mode-000
    untracked file and the correction rewrites it while permissions stay unreadable,
    both probes would hash identical ``<missing>`` markers and accept a non-FIXED verdict.
    """
    worktree = tmp_path / "ws_unreadable_untracked"
    worktree.mkdir()
    (worktree / "src").mkdir()
    target = worktree / "src" / "secret.py"
    target.write_text("attempt0\n", encoding="utf-8")

    real_open = Path.open

    def _permission_denied_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == target and args and args[0] == "rb":
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _permission_denied_open)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="?? src/secret.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_untracked",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_untracked",
        worktree_path=worktree,
    )
    assert correction_fp is None
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
def test_git_index_blob_sha_returns_none_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_index_blob_sha(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_index_blob_sha_resolves_stage_like_filenames(
    tmp_path: Path,
) -> None:
    """Filenames like ``0:x`` must not be parsed as Git's ``:<stage>:<path>`` syntax.

    Production regression for PRRT_kwDOSJAM6s6eQcs6: ``:{path}`` for ``0:x`` becomes
    ``:0:x`` (stage 0 of ``x``), returning None and colliding fingerprints.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree(worktree)
    stage_like = worktree / "0:x"
    stage_like.write_text("stage-like\n", encoding="utf-8")
    subprocess.run(["git", "add", "0:x"], cwd=worktree, check=True, capture_output=True)
    expected = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", ":0:./0:x"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        comment_verdict_residue._git_index_blob_sha(
            worktree_path=worktree,
            path="0:x",
            git_env={},
        )
        == expected
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_returns_none_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "sub").mkdir()

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_symlink_hash_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    target = worktree / "target.txt"
    target.write_text("payload\n", encoding="utf-8")
    symlink = worktree / "src" / "link"
    symlink.symlink_to(target)

    real_run = comment_verdict_residue._run_git_bytes

    def _fail_stdin(**kwargs: object) -> subprocess.CompletedProcess[bytes]:
        args = kwargs.get("args", ())
        if args and "hash-object" in args and "--stdin" in args:
            return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")
        return real_run(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail_stdin)
    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/link",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_readlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    link_path = worktree / "src" / "link"
    link_path.parent.mkdir(parents=True)
    link_path.symlink_to("target")

    real_readlink = Path.readlink

    def _raise_readlink(self: Path) -> Path:
        if self == link_path:
            raise OSError("readlink failed")
        return real_readlink(self)

    monkeypatch.setattr(Path, "readlink", _raise_readlink)
    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/link",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_regular_file_hash_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "x.py").write_text("payload\n", encoding="utf-8")

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_index_mode_returns_none_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_index_mode(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_index_mode_returns_none_on_empty_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _empty(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _empty)
    assert (
        comment_verdict_residue._git_index_mode(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_mode_lstat_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = worktree / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("payload\n", encoding="utf-8")

    real_lstat = Path.lstat

    def _raise_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            raise OSError("lstat failed")
        return real_lstat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "lstat", _raise_lstat)
    assert (
        comment_verdict_residue._git_worktree_mode(
            worktree_path=worktree,
            path="src/x.py",
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_mode_non_regular_returns_none(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    assert (
        comment_verdict_residue._git_worktree_mode(
            worktree_path=worktree,
            path="src",
        )
        is None
    )


@pytest.mark.unit
def test_hash_tracked_residue_diffs_protected_scope_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _name_only(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=b"protected/path\x00",
            stderr=b"",
        )

    def _protected_scope(_stdout: bytes) -> list[str]:
        raise ProtectedScopeDiffError("blocked")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _name_only)
    monkeypatch.setattr(
        comment_verdict_residue,
        "_changed_paths_from_name_only_z",
        _protected_scope,
    )
    assert (
        comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )
