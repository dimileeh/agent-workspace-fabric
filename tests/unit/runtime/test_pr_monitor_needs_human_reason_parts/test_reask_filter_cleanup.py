"""Filter isolation and failed-creation cleanup regressions for re-asks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import comment_verdict, comments
from awf.runtime.pr_monitor_runner.comments_source_git import (
    _reask_source_mirror_command,
    _rev_parse_pinned_reask_source_head,
)


@pytest.mark.unit
async def test_checkout_filter_overrides_disable_every_detected_filter_driver() -> None:
    """An isolated checkout disables both smudge and process filters for each driver."""

    class _FilterRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            return CommandResult(
                returncode=0,
                stdout="filter.lfs.smudge\nfilter.lfs.process\nfilter.custom.process\n",
                stderr="",
            )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_FilterRunner()))

    assert await comments._checkout_filter_overrides(  # noqa: SLF001
        runner,
        worktree_path=Path("/worktree"),
    ) == (
        "-c",
        "filter.custom.smudge=",
        "-c",
        "filter.custom.process=",
        "-c",
        "filter.custom.required=false",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.required=false",
    )


@pytest.mark.unit
async def test_checkout_filter_overrides_accepts_no_configured_filters() -> None:
    """A worktree without checkout filters can be populated without extra Git options."""

    class _NoFiltersRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            return CommandResult(returncode=1, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_NoFiltersRunner()))

    assert (
        await comments._checkout_filter_overrides(  # noqa: SLF001
            runner,
            worktree_path=Path("/worktree"),
        )
        == ()
    )


@pytest.mark.unit
async def test_create_isolated_reask_ignores_non_worktree_path() -> None:
    """A lightweight runner without a Git worktree cannot create a sibling checkout."""
    runner = SimpleNamespace()

    assert (
        await comments._create_isolated_reask_worktree(  # noqa: SLF001
            runner,
            worktree_path=Path("/not-a-worktree"),
            restore_ref="a" * 40,
        )
        is None
    )


@pytest.mark.unit
def test_unknown_review_item_has_no_persisted_body_state_key() -> None:
    """Only supported review item types influence the addressed-state ledger."""
    assert comments._review_item_body_state_key("item_1", "commit") is None  # noqa: SLF001


@pytest.mark.unit
async def test_failed_creation_cleanup_surfaces_runner_exception() -> None:
    """A checkout potentially created before a runner error remains a terminal cleanup failure."""

    class _FailingRemoveRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            raise OSError("worktree remove transport failed")

    reask_worktree = comments._IsolatedReaskWorktree(  # noqa: SLF001
        source_worktree=Path("/worktree"),
        path=Path("/reask"),
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_FailingRemoveRunner()))

    assert (
        await comments._cleanup_isolated_reask_worktree_after_creation_failure(  # noqa: SLF001
            runner,
            reask_worktree=reask_worktree,
            event_name="test.reask.cleanup_failed",
        )
        == "worktree remove transport failed"
    )


@pytest.mark.unit
async def test_pinned_reask_head_read_returns_the_pinned_revision() -> None:
    """A successful source-Git probe returns its exact HEAD without reading the worktree."""
    expected_ref = "a" * 40

    class _HeadRunner:
        def __init__(self) -> None:
            self.args: list[str] | None = None
            self.timeout_seconds: float | None = None

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float,
            **_kwargs: object,
        ) -> CommandResult:
            self.args = args
            self.timeout_seconds = timeout_seconds
            return CommandResult(returncode=0, stdout=f"{expected_ref}\n", stderr="")

    command_runner = _HeadRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    assert (
        await _rev_parse_pinned_reask_source_head(
            runner,
            Path("/worktrees/ws_1/.git/worktrees/reask"),
            head_snapshot="ref: refs/heads/awf/ws_1\n",
            timeout_seconds=30.0,
        )
        == expected_ref
    )
    assert command_runner.args == [
        "git",
        "--git-dir",
        "/worktrees/ws_1/.git/worktrees/reask",
        "rev-parse",
        "--verify",
        "--end-of-options",
        "refs/heads/awf/ws_1^{commit}",
    ]
    assert command_runner.timeout_seconds == 30.0


@pytest.mark.unit
async def test_pinned_reask_head_read_returns_none_for_git_failure() -> None:
    """A failed pinned HEAD read leaves clarification unavailable rather than guessing a ref."""

    class _FailedHeadRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            return CommandResult(returncode=1, stdout="", stderr="missing linked Git directory")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_FailedHeadRunner()))

    assert (
        await _rev_parse_pinned_reask_source_head(
            runner,
            Path("/worktrees/ws_1/.git/worktrees/reask"),
            head_snapshot="ref: refs/heads/awf/ws_1\n",
            timeout_seconds=30.0,
        )
        is None
    )


@pytest.mark.unit
async def test_pinned_reask_head_read_accepts_a_detached_head_snapshot() -> None:
    """A detached source worktree resolves its captured object ID directly."""
    expected_ref = "b" * 40

    class _HeadRunner:
        def __init__(self) -> None:
            self.args: list[str] | None = None

        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            self.args = args
            return CommandResult(returncode=0, stdout=f"{expected_ref}\n", stderr="")

    command_runner = _HeadRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    assert (
        await _rev_parse_pinned_reask_source_head(
            runner,
            Path("/worktrees/ws_1/.git/worktrees/reask"),
            head_snapshot=f"{expected_ref}\n",
            timeout_seconds=30.0,
        )
        == expected_ref
    )
    assert command_runner.args is not None
    assert command_runner.args[-1] == f"{expected_ref}^{{commit}}"


@pytest.mark.unit
@pytest.mark.parametrize("head_snapshot", ["ref: HEAD\n", "not a commit\n", "z" * 40])
async def test_pinned_reask_head_read_rejects_unsafe_head_snapshots(head_snapshot: str) -> None:
    """Malformed snapshots must not cause Git to reopen a mutable HEAD alias."""

    class _UnexpectedRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            pytest.fail("an unsafe HEAD snapshot must not invoke Git")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_UnexpectedRunner()))

    assert (
        await _rev_parse_pinned_reask_source_head(
            runner,
            Path("/worktrees/ws_1/.git/worktrees/reask"),
            head_snapshot=head_snapshot,
            timeout_seconds=30.0,
        )
        is None
    )


@pytest.mark.unit
def test_reask_source_mirror_command_uses_pinned_mirror_when_available() -> None:
    """Re-ask worktree operations target the pinned bare mirror instead of mutable source metadata."""
    assert _reask_source_mirror_command(
        Path("/worktrees/ws_1"),
        Path("/mirrors/repository.git"),
        "worktree",
        "remove",
        "/reask",
    ) == [
        "git",
        "--git-dir",
        "/mirrors/repository.git",
        "worktree",
        "remove",
        "/reask",
    ]


@pytest.mark.unit
async def test_owned_paths_lookup_failure_does_not_block_comment_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable workspace metadata gives the comment agent an empty path scope."""

    async def _unavailable_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        raise RuntimeError("workspace repository unavailable")

    monkeypatch.setattr(comment_verdict, "_owned_paths_for_prompt", _unavailable_owned_paths)

    assert (
        await comment_verdict._owned_paths_for_prompt_or_empty(  # noqa: SLF001
            SimpleNamespace(),
            "ws_1",
        )
        == []
    )
