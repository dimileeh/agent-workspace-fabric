"""Filter isolation and failed-creation cleanup regressions for re-asks."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor_runner import comment_verdict, comments, comments_checkout
from awf.runtime.pr_monitor_runner.comments_source_git import (
    _reask_source_mirror_command,
    _rev_parse_pinned_reask_source_head,
)
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError


@pytest.mark.unit
async def test_checkout_filter_overrides_fail_closed_when_metadata_lookup_fails() -> None:
    """Unreadable tracked metadata cannot lead to an unsafe checkout."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=2, stderr="tree unreadable")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not read tracked checkout filters"):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )


@pytest.mark.unit
async def test_checkout_filter_overrides_fail_closed_when_attribute_blob_read_fails() -> None:
    """An unreadable pinned attributes blob cannot lead to an unsafe checkout."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=0, stdout=".gitattributes\0")
    command_runner.queue_result(returncode=2, stderr="attributes unreadable")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not read tracked checkout filters"):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )


@pytest.mark.unit
async def test_checkout_filter_overrides_reject_unexpected_attribute_driver() -> None:
    """A malformed attribute driver cannot be passed into the host Git command."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=0, stdout=".gitattributes\0")
    command_runner.queue_result(returncode=0, stdout="*.txt filter=poison/unsafe\n")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(
        _MonitorPolicyBlockedError, match="Could not safely disable checkout filters"
    ):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )


@pytest.mark.unit
async def test_checkout_filter_overrides_disable_every_detected_filter_driver() -> None:
    """An isolated checkout disables both smudge and process filters for each driver."""

    class _FilterRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.results = iter(
                (
                    CommandResult(
                        returncode=0,
                        stdout=".gitattributes\0nested/.gitattributes\0",
                        stderr="",
                    ),
                    CommandResult(returncode=0, stdout="*.bin filter=lfs\n", stderr=""),
                    CommandResult(returncode=0, stdout="*.txt filter=custom\n", stderr=""),
                )
            )

        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            self.commands.append(_args)
            return next(self.results)

    command_runner = _FilterRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    assert await comments_checkout._checkout_filter_overrides(  # noqa: SLF001
        runner,
        worktree_path=Path("/worktree"),
        restore_ref="a" * 40,
        source_mirror=Path("/trusted/mirror.git"),
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
    assert command_runner.commands[0][:3] == ["git", "--git-dir", "/trusted/mirror.git"]


@pytest.mark.unit
async def test_checkout_filter_overrides_accepts_no_tracked_filters() -> None:
    """A worktree without tracked checkout filters needs no extra Git options."""

    class _NoFiltersRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_NoFiltersRunner()))

    assert (
        await comments_checkout._checkout_filter_overrides(  # noqa: SLF001
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )
        == ()
    )


@pytest.mark.unit
async def test_checkout_filter_overrides_rejects_excessive_tracked_attribute_files() -> None:
    """A tree with too many attributes files cannot start unbounded Git probes."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(
        stdout="\0".join(f"directory_{index}/.gitattributes" for index in range(129))
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not read tracked checkout filters"):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )

    assert len(command_runner.calls) == 1


@pytest.mark.unit
async def test_checkout_filter_overrides_rejects_excessive_tree_output() -> None:
    """A large tree listing is rejected before it can drive any attribute reads."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(stdout="x" * (1024 * 1024 + 1))
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not read tracked checkout filters"):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )

    assert len(command_runner.calls) == 1


@pytest.mark.unit
async def test_checkout_filter_overrides_uses_one_deadline_for_tree_and_attribute_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attributes read spends from the same discovery budget as the tree probe."""

    class _Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    class _AdvancingRunner:
        def __init__(self, clock: _Clock) -> None:
            self.clock = clock
            self.timeouts: list[float | None] = []
            self.results = iter(
                (
                    CommandResult(returncode=0, stdout=".gitattributes\0", stderr=""),
                    CommandResult(returncode=0, stdout="*.txt filter=custom\n", stderr=""),
                )
            )

        async def run(
            self,
            _args: list[str],
            *,
            timeout_seconds: float | None = None,
            **_kwargs: object,
        ) -> CommandResult:
            self.timeouts.append(timeout_seconds)
            self.clock.now += 10.0
            return next(self.results)

    clock = _Clock()
    command_runner = _AdvancingRunner(clock)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    monkeypatch.setattr(comments_checkout, "time", SimpleNamespace(monotonic=clock.monotonic))

    assert await comments_checkout._checkout_filter_overrides(
        runner,
        worktree_path=Path("/worktree"),
        restore_ref="a" * 40,
        source_mirror=None,
    ) == (
        "-c",
        "filter.custom.smudge=",
        "-c",
        "filter.custom.process=",
        "-c",
        "filter.custom.required=false",
    )
    assert command_runner.timeouts == [30.0, 20.0]


@pytest.mark.unit
async def test_checkout_filter_overrides_rejects_tree_probe_that_exhausts_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree probe that reaches the shared deadline cannot permit a checkout."""

    class _Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    class _DeadlineRunner:
        def __init__(self, clock: _Clock) -> None:
            self.clock = clock
            self.timeouts: list[float | None] = []

        async def run(
            self,
            _args: list[str],
            *,
            timeout_seconds: float | None = None,
            **_kwargs: object,
        ) -> CommandResult:
            self.timeouts.append(timeout_seconds)
            self.clock.now += 30.0
            return CommandResult(returncode=0, stdout=".gitattributes\0", stderr="")

    clock = _Clock()
    command_runner = _DeadlineRunner(clock)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    monkeypatch.setattr(comments_checkout, "time", SimpleNamespace(monotonic=clock.monotonic))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not read tracked checkout filters"):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )

    assert command_runner.timeouts == [30.0]


@pytest.mark.unit
async def test_checkout_filter_overrides_does_not_start_attribute_read_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delay after tree discovery cannot begin a fresh per-file timeout."""

    class _Clock:
        def __init__(self) -> None:
            self.values = iter((100.0, 100.0, 129.0, 130.0))

        def monotonic(self) -> float:
            return next(self.values)

    class _TreeRunner:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        async def run(
            self,
            _args: list[str],
            *,
            timeout_seconds: float | None = None,
            **_kwargs: object,
        ) -> CommandResult:
            self.timeouts.append(timeout_seconds)
            return CommandResult(returncode=0, stdout=".gitattributes\0", stderr="")

    command_runner = _TreeRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    monkeypatch.setattr(
        comments_checkout,
        "time",
        SimpleNamespace(monotonic=_Clock().monotonic),
    )

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not read tracked checkout filters"):
        await comments_checkout._checkout_filter_overrides(
            runner,
            worktree_path=Path("/worktree"),
            restore_ref="a" * 40,
            source_mirror=None,
        )

    assert command_runner.timeouts == [30.0]


@pytest.mark.unit
def test_checkout_info_attributes_filter_overrides_rejects_symlinked_attributes(
    tmp_path: Path,
) -> None:
    """A mutable attributes symlink cannot redirect the checkout guard's read."""
    git_dir = tmp_path / "mirror.git"
    (git_dir / "info").mkdir(parents=True)
    target = tmp_path / "outside-attributes"
    target.write_text("*.txt filter=poison\n", encoding="utf-8")
    (git_dir / "info" / "attributes").symlink_to(target)

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not safely read checkout info"):
        comments_checkout._checkout_info_attributes_filter_overrides(  # noqa: SLF001
            source_mirror=git_dir,
            source_worktree_path=tmp_path / "worktree",
        )


@pytest.mark.unit
def test_checkout_info_attributes_filter_overrides_rejects_special_attributes(
    tmp_path: Path,
) -> None:
    """A mutable attributes FIFO cannot block or direct the checkout guard."""
    git_dir = tmp_path / "mirror.git"
    (git_dir / "info").mkdir(parents=True)
    os.mkfifo(git_dir / "info" / "attributes")

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not safely read checkout info"):
        comments_checkout._checkout_info_attributes_filter_overrides(  # noqa: SLF001
            source_mirror=git_dir,
            source_worktree_path=tmp_path / "worktree",
        )


@pytest.mark.unit
def test_checkout_info_attributes_filter_overrides_rejects_oversized_attributes(
    tmp_path: Path,
) -> None:
    """A mutable attributes file cannot exhaust memory before checkout."""
    git_dir = tmp_path / "mirror.git"
    (git_dir / "info").mkdir(parents=True)
    (git_dir / "info" / "attributes").write_bytes(
        b"#" * (comments_checkout._MAX_CHECKOUT_INFO_ATTRIBUTES_BYTES + 1)  # noqa: SLF001
    )

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not safely read checkout info"):
        comments_checkout._checkout_info_attributes_filter_overrides(  # noqa: SLF001
            source_mirror=git_dir,
            source_worktree_path=tmp_path / "worktree",
        )


@pytest.mark.unit
def test_checkout_info_attributes_filter_overrides_rejects_non_utf8_attributes(
    tmp_path: Path,
) -> None:
    """An undecodable mutable attributes file fails closed before checkout."""
    git_dir = tmp_path / "mirror.git"
    (git_dir / "info").mkdir(parents=True)
    (git_dir / "info" / "attributes").write_bytes(b"*.txt filter=poison\xff\n")

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not safely read checkout info"):
        comments_checkout._checkout_info_attributes_filter_overrides(  # noqa: SLF001
            source_mirror=git_dir,
            source_worktree_path=tmp_path / "worktree",
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
            head_snapshot=expected_ref,
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
        f"{expected_ref}^{{commit}}",
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
            head_snapshot="a" * 40,
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
@pytest.mark.parametrize(
    "head_snapshot",
    ["ref: HEAD\n", "ref: refs/heads/awf/ws_1\n", "not a commit\n", "z" * 40],
)
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
