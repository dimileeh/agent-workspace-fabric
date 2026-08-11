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

import contextlib
import os
import stat
from pathlib import Path
from typing import Any, Final

from awf.common.logging import get_logger
from awf.runtime.validation_worktree import GitRunner

_log = get_logger(__name__)

AWF_SCRATCH_BLOCK_START = "# >>> awf-managed agent scratch"
AWF_SCRATCH_BLOCK_END = "# <<< awf-managed agent scratch"

_SAFE_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_SAFE_EXCLUDE_OPEN_FLAGS = (
    os.O_RDWR
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_MAX_SCRATCH_EXCLUDE_BYTES: Final = 1_048_576


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


def _extract_managed_patterns(content: str) -> tuple[str, ...]:
    """Return the patterns currently inside the AWF-managed block, in order.

    Because AWF uses bare mirrors with linked worktrees, ``info/exclude`` is
    shared across every worktree of a repo. Preserving the existing block's
    patterns lets ``apply_agent_scratch_excludes`` *union* a second adapter's
    paths with those already present instead of clobbering them — so the last
    writer never drops another agent's exclusions. Tolerant of a missing block
    or a block missing its end sentinel (read to EOF).
    """
    patterns: list[str] = []
    in_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped == AWF_SCRATCH_BLOCK_START:
                in_block = True
            continue
        if stripped == AWF_SCRATCH_BLOCK_END:
            break
        if stripped:
            patterns.append(stripped)
    return tuple(patterns)


def _render_managed_block(patterns: tuple[str, ...]) -> str:
    """Render the sentinel-wrapped block, one pattern per line, order-stable."""
    deduped = list(dict.fromkeys(patterns))
    body = "\n".join(deduped)
    return f"{AWF_SCRATCH_BLOCK_START}\n{body}\n{AWF_SCRATCH_BLOCK_END}\n"


def _open_scratch_directory(path: Path) -> int:
    """Open or create a directory path without following any component links."""
    if path.is_absolute():
        directory_fd = os.open(path.anchor, _SAFE_DIRECTORY_OPEN_FLAGS)
        parts = path.parts[1:]
    else:
        directory_fd = os.open(".", _SAFE_DIRECTORY_OPEN_FLAGS)
        parts = path.parts

    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise OSError(f"unsafe Git scratch exclude directory path: {path}")
            try:
                child_fd = os.open(part, _SAFE_DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=directory_fd)
                child_fd = os.open(part, _SAFE_DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
    except OSError:
        with contextlib.suppress(OSError):
            os.close(directory_fd)
        raise
    return directory_fd


def _open_regular_scratch_exclude(exclude_path: Path) -> int:
    """Open ``info/exclude`` safely, rejecting symlinks and special files.

    The preceding agent can write shared mirror metadata before a re-ask starts.
    Anchor the exclude leaf at a no-follow ``info`` descriptor so a swapped
    directory or leaf cannot redirect the control-plane write or make it block.
    """
    info_fd = _open_scratch_directory(exclude_path.parent)
    try:
        try:
            exclude_fd = os.open(
                exclude_path.name,
                _SAFE_EXCLUDE_OPEN_FLAGS,
                dir_fd=info_fd,
            )
        except FileNotFoundError:
            exclude_fd = os.open(
                exclude_path.name,
                _SAFE_EXCLUDE_OPEN_FLAGS | os.O_CREAT | os.O_EXCL,
                0o666,
                dir_fd=info_fd,
            )
    finally:
        os.close(info_fd)

    try:
        exclude_stat = os.fstat(exclude_fd)
        if not stat.S_ISREG(exclude_stat.st_mode):
            raise OSError(f"Git scratch exclude is not a regular file: {exclude_path}")
        if exclude_stat.st_size > _MAX_SCRATCH_EXCLUDE_BYTES:
            raise OSError(f"Git scratch exclude exceeds size limit: {exclude_path}")
    except OSError:
        with contextlib.suppress(OSError):
            os.close(exclude_fd)
        raise
    return exclude_fd


def _rewrite_scratch_exclude(exclude_path: Path, scratch_paths: tuple[str, ...]) -> None:
    """Rewrite one safely-opened exclude descriptor with AWF's managed block."""
    exclude_fd = _open_regular_scratch_exclude(exclude_path)
    try:
        existing_bytes = bytearray()
        while len(existing_bytes) <= _MAX_SCRATCH_EXCLUDE_BYTES:
            chunk = os.read(
                exclude_fd,
                min(64 * 1024, _MAX_SCRATCH_EXCLUDE_BYTES + 1 - len(existing_bytes)),
            )
            if not chunk:
                break
            existing_bytes.extend(chunk)
        if len(existing_bytes) > _MAX_SCRATCH_EXCLUDE_BYTES:
            raise OSError(f"Git scratch exclude exceeds size limit: {exclude_path}")
        existing = existing_bytes.decode("utf-8")
        prefix = _strip_managed_block(existing)
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        # Union with any patterns a different adapter already wrote to this
        # shared (linked-worktree) exclude file so a *sequential* second adapter
        # does not clobber the first's scratch exclusions (``_render`` dedups).
        # Under truly concurrent different-adapter writes this read-modify-write
        # is racy; see the docstring for the bounded, self-healing tradeoff.
        merged = _extract_managed_patterns(existing) + scratch_paths
        rendered = (prefix + _render_managed_block(merged)).encode("utf-8")
        os.lseek(exclude_fd, 0, os.SEEK_SET)
        os.ftruncate(exclude_fd, 0)
        written = 0
        while written < len(rendered):
            count = os.write(exclude_fd, rendered[written:])
            if count == 0:
                raise OSError("could not write Git scratch exclude")
            written += count
    finally:
        with contextlib.suppress(OSError):
            os.close(exclude_fd)


def _path_is_within_root(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves beneath ``root`` without trusting its spelling."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


async def apply_agent_scratch_excludes(
    *,
    run_git: GitRunner,
    worktree_path: Path,
    scratch_paths: tuple[str, ...],
    validated_mirror_path: Path | None = None,
    logger: Any = _log,
) -> bool:
    """Add an agent's runtime scratch paths to the worktree's git exclude file.

    Returns ``True`` when the exclude file was (re)written, ``False`` otherwise.
    A ``False`` return is never an error the caller must handle — an agent with
    no scratch paths is a deliberate no-op, and a failed ``rev-parse`` or an
    unwritable exclude file is logged and degraded gracefully so provisioning
    and validation proceed (the guard then simply behaves as it did before).

    ``validated_mirror_path`` is the previously validated source mirror for a
    pinned review re-ask. When supplied, Git's current path calculation must
    remain beneath that mirror; a changed linked-worktree ``commondir`` must
    not redirect this control-plane write to another workspace's exclude file.

    Concurrency: the read-modify-write below is not atomic, and ``info/exclude``
    is shared across every linked worktree of a bare mirror. For *same-agent*
    concurrent runs this is harmless (both writers render identical content). For
    *different-adapter* runs on the same mirror there is a narrow window where
    two writers both read the pre-existing block and the last writer drops the
    other's patterns; the loser re-applies on its next cycle (executor pre-agent
    or monitor pre-push), so the worst case is a transient guard miss, not lost
    state. This is a deliberate tradeoff — AWF does not serialize exclude writes.
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
    if validated_mirror_path is not None and not _path_is_within_root(
        exclude_path, validated_mirror_path
    ):
        logger.warning(
            "agent_scratch.exclude_path_outside_validated_mirror",
            worktree_path=str(worktree_path),
            exclude_path=str(exclude_path),
            validated_mirror_path=str(validated_mirror_path),
        )
        return False

    try:
        _rewrite_scratch_exclude(exclude_path, scratch_paths)
    except (OSError, ValueError) as exc:
        # ``ValueError`` covers ``UnicodeDecodeError`` (a ``ValueError`` subclass)
        # raised while decoding non-UTF-8 ``info/exclude`` bytes — degrade
        # gracefully like any other exclude I/O failure.
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
