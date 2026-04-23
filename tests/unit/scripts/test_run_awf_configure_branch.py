"""Tests for ``scripts.run_awf._configure_branch_push_upstream``.

Regression guard for the 2026-04-23 aira-web incident. The function
used to set ``push.default = upstream`` as a third git-config write.
On a bare-mirror multi-worktree layout that config is GLOBAL — every
other worktree sharing the mirror reads it — and it combined with
auto-set ``branch.<X>.merge=refs/heads/development`` to redirect
feature-branch pushes onto ``development``. Four commits landed on
aira-web ``development`` this way, bypassing all review.

The fix: the monitor now pushes via an explicit
``HEAD:refs/heads/<remote>`` refspec and no longer relies on
``push.default``. This function must NEVER set ``push.default`` again
— these tests enforce that invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scripts.run_awf import _configure_branch_push_upstream


@dataclass
class _RecordedResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class _RecordedCall:
    args: list[str]


@dataclass
class _FakeRunner:
    """Minimal stand-in for ``AsyncioSubprocessRunner`` — records every
    invocation so tests can assert on the exact git config writes."""

    calls: list[_RecordedCall] = field(default_factory=list)

    async def run(self, args: list[str], **_kwargs: Any) -> _RecordedResult:
        self.calls.append(_RecordedCall(args=list(args)))
        return _RecordedResult(returncode=0)


class TestConfigureBranchPushUpstream:
    @pytest.mark.unit
    async def test_writes_only_remote_and_merge_configs(self) -> None:
        runner = _FakeRunner()
        await _configure_branch_push_upstream(
            runner=runner,  # type: ignore[arg-type]
            worktree_path=Path("/tmp/worktree"),
            branch_name="release-sync/ws_abc",
            remote_branch="development",
        )
        # Exactly two git config writes — no third one for push.default.
        assert len(runner.calls) == 2
        all_args = [c.args for c in runner.calls]
        assert all_args[0] == [
            "git",
            "-C",
            "/tmp/worktree",
            "config",
            "branch.release-sync/ws_abc.remote",
            "origin",
        ]
        assert all_args[1] == [
            "git",
            "-C",
            "/tmp/worktree",
            "config",
            "branch.release-sync/ws_abc.merge",
            "refs/heads/development",
        ]

    @pytest.mark.unit
    async def test_never_writes_push_default(self) -> None:
        """The 2026-04-23 regression guard. Any reintroduction of
        ``push.default`` — global or scoped — fails this test."""
        runner = _FakeRunner()
        await _configure_branch_push_upstream(
            runner=runner,  # type: ignore[arg-type]
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature-sync/ws_xyz",
            remote_branch="fix/some-head-branch",
        )
        for call in runner.calls:
            assert "push.default" not in call.args, (
                f"_configure_branch_push_upstream must NOT set push.default. "
                f"Reintroducing this write re-opens the 2026-04-23 aira-web "
                f"incident where feature-branch pushes got redirected to "
                f"development via polluted shared-mirror config. "
                f"Full args: {call.args}"
            )

    @pytest.mark.unit
    async def test_branch_config_uses_refs_heads_prefix(self) -> None:
        """The ``branch.<X>.merge`` value must be a fully-qualified ref
        (``refs/heads/<branch>``). Shortcut forms like ``<branch>`` or
        just the branch name are technically accepted by git but confuse
        tools that read the config back out."""
        runner = _FakeRunner()
        await _configure_branch_push_upstream(
            runner=runner,  # type: ignore[arg-type]
            worktree_path=Path("/tmp/w"),
            branch_name="feature-sync/abc",
            remote_branch="fix/upstream-branch",
        )
        merge_write = next(
            c for c in runner.calls if "branch.feature-sync/abc.merge" in c.args
        )
        assert "refs/heads/fix/upstream-branch" in merge_write.args
