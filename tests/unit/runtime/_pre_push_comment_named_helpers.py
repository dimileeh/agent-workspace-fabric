"""Shared probe builders for the comment-named target evidence tests (issue #1232).

Extracted from ``test_pre_push_validation_fix_pass_ancestry_comment_named.py`` so
that module stays under the first-party file line limit enforced by
``tests/unit/test_core_decomposition_maintainability.py``.
"""

from __future__ import annotations

from pathlib import Path

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_comment_named as comment_named,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import _LEFT, _RIGHT, _Probe

_DOC = "docs/runbooks/vm-core-update.md"
_ROUTES = "src/app/api/routes/team.py"
_INBOUND = "src/app/messaging/inbound_types.py"
_CHANNEL = "src/app/agents_management/channel_service.py"
_WEB = "src/app/web/team.ts"
_OTHER = "src/app/control/queue_sweeper.py"

_GrepOutput = tuple[tuple[str, str], ...] | str | None


def _diff(path: str, header: str, *body: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n{header}\n" + "".join(f"{line}\n" for line in body)


class _GrepProbe(_Probe):
    """A ``_Probe`` that also answers the repo-wide definition ``git grep``.

    ``definitions`` maps a ref to the ``(path, matched line)`` records Git would
    print for it, or to raw output; a ref absent from it greps nothing (exit 1),
    and a ``None`` value is an unreadable grep. ``pattern_files`` likewise answers
    the ``-l`` grep for files holding a ``case`` clause and a name: a ref absent
    from it lists nothing, a ``None`` value fails, and a string is raw output.
    """

    def __init__(
        self,
        *,
        definitions: dict[str, _GrepOutput],
        pattern_files: dict[str, tuple[str, ...] | str | None],
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.definitions = definitions
        self.pattern_files = pattern_files
        self.greps: list[str] = []

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        if "grep" not in cmd:
            return await super()._run(cmd, **kwargs)
        ref = cmd[cmd.index("--") - 1]
        if "-l" in cmd:
            if ref not in self.pattern_files:
                return CommandResult(returncode=1, stdout="", stderr="")
            listed = self.pattern_files[ref]
            if listed is None:
                return CommandResult(returncode=128, stdout="", stderr="grep failed")
            stdout = listed if isinstance(listed, str) else "".join(f"{ref}:{p}\0" for p in listed)
            return CommandResult(returncode=0, stdout=stdout, stderr="")
        self.greps.append(ref)
        if ref not in self.definitions:
            return CommandResult(returncode=1, stdout="", stderr="")
        records = self.definitions[ref]
        if records is None:
            return CommandResult(returncode=128, stdout="", stderr="grep failed")
        stdout = (
            records
            if isinstance(records, str)
            else "".join(f"{ref}:{path}\0{line}\n" for path, line in records)
        )
        return CommandResult(returncode=0, stdout=stdout, stderr="")


def _probe(
    *,
    texts: dict[str, str | None] | None = None,
    changed_paths: tuple[str, ...] | None,
    diffs: dict[str, str | None] | None = None,
    definitions: dict[str, _GrepOutput] | None = None,
    pattern_files: dict[str, tuple[str, ...] | str | None] | None = None,
) -> _GrepProbe:
    return _GrepProbe(
        texts={(_LEFT, path): text for path, text in (texts or {}).items()},
        changed_paths=changed_paths,
        diffs=dict(diffs or {}),
        definitions=dict(definitions or {}),
        pattern_files=dict(pattern_files or {}),
    )


async def _changes_named_target(runner: object, comment_text: str) -> bool:
    return await comment_named._commit_range_changes_comment_named_target(
        runner,
        worktree_path=Path("/tmp/ws"),
        left=_LEFT,
        right=_RIGHT,
        comment_text=comment_text,
    )
