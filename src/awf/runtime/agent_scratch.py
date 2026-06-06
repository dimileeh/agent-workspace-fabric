"""Git-native exclusion of agent-runtime scratch paths from validation.

Some coding CLIs create checkout-local scratch directories while they run —
``claude_code`` creates ``.claude/worktrees/`` (nested git worktrees for its
isolated subagents). That state is agent-runtime noise, not AWF artifacts and
not project work, but AWF's validation-cleanliness guard
(``runtime/validation_worktree.check_validation_worktree_clean``) refuses to run
AWF-owned validation from a tree with untracked, non-ignored paths.

Rather than depend on the target repo gitignoring those paths, AWF writes them
to the worktree's *local* git exclude file (``$GIT_DIR/info/exclude``) inside an
idempotent, clearly-marked AWF-managed block. This is git-native, never touches
the target repo's committed ``.gitignore``, and makes ``git status --ignored``
report the scratch as ignored — so the guard (which trusts git's own ignore
view) simply stops seeing it as dirty. The guard's policy is unchanged.

The set of scratch paths is declared *with the agent adapter*
(``AgentAdapter.runtime_scratch_paths``) so AWF core stays generic: every agent
that needs an exclusion carries its own list, and agents with no scratch paths
are a no-op here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.common.commands import CommandResult
from awf.common.logging import get_logger

_log = get_logger(__name__)

GitRunner = Callable[[list[str]], Awaitable[CommandResult]]

AWF_SCRATCH_BLOCK_START = "# >>> awf-managed agent scratch"
AWF_SCRATCH_BLOCK_END = "# <<< awf-managed agent scratch"


def _strip_managed_block(content: str) -> str:
    """Return ``content`` with any AWF-managed scratch block removed.

    Tolerant of a missing block, a block missing its end sentinel (everything
    from the start sentinel onward is dropped), and repeated calls — so the
    rewrite never accumulates duplicate blocks. Non-AWF lines are preserved.
    """
    kept: list[str] = []
    in_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if not in_block and stripped == AWF_SCRATCH_BLOCK_START:
            in_block = True
            continue
        if in_block:
            if stripped == AWF_SCRATCH_BLOCK_END:
                in_block = False
            continue
        kept.append(line)
    return "\n".join(kept)


def _render_managed_block(patterns: tuple[str, ...]) -> str:
    """Render the sentinel-wrapped block, one pattern per line, order-stable."""
    deduped = list(dict.fromkeys(patterns))
    body = "\n".join(deduped)
    return f"{AWF_SCRATCH_BLOCK_START}\n{body}\n{AWF_SCRATCH_BLOCK_END}\n"


async def apply_agent_scratch_excludes(
    *,
    run_git: GitRunner,
    worktree_path: Path,
    scratch_paths: tuple[str, ...],
    logger: Any = _log,
) -> bool:
    """Add an agent's runtime scratch paths to the worktree's git exclude file.

    Returns ``True`` when the exclude file was (re)written, ``False`` otherwise.
    A ``False`` return is never an error the caller must handle — an agent with
    no scratch paths is a deliberate no-op, and a failed ``rev-parse`` or an
    unwritable exclude file is logged and degraded gracefully so provisioning
    and validation proceed (the guard then simply behaves as it did before).
    """
    if not scratch_paths:
        return False

    rev_parse = await run_git(["rev-parse", "--git-path", "info/exclude"])
    if not rev_parse.ok:
        logger.warning(
            "agent_scratch.exclude_rev_parse_failed",
            worktree_path=str(worktree_path),
            returncode=rev_parse.returncode,
            stderr=(rev_parse.stderr or "")[:500],
        )
        return False

    raw_path = (rev_parse.stdout or "").splitlines()[0].strip() if rev_parse.stdout else ""
    if not raw_path:
        logger.warning(
            "agent_scratch.exclude_path_empty",
            worktree_path=str(worktree_path),
        )
        return False

    exclude_path = Path(raw_path)
    if not exclude_path.is_absolute():
        exclude_path = worktree_path / exclude_path

    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        prefix = _strip_managed_block(existing)
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        exclude_path.write_text(prefix + _render_managed_block(scratch_paths), encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "agent_scratch.exclude_write_failed",
            worktree_path=str(worktree_path),
            exclude_path=str(exclude_path),
            error=str(exc),
        )
        return False

    logger.info(
        "agent_scratch.exclude_applied",
        worktree_path=str(worktree_path),
        exclude_path=str(exclude_path),
        scratch_paths=list(scratch_paths),
    )
    return True
