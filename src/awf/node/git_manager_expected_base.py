"""Resolve a named remote branch to one immutable commit before worktree add."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any, Protocol

from awf.node import git_manager_mirror as _git_manager_mirror


class _GitRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        operation: str,
        env: Mapping[str, str] | None = None,
    ) -> Awaitable[Any]: ...


async def locked_worktree_start_point(
    run: _GitRunner,
    *,
    mirror_path: Path,
    base_branch: str,
    tracking_ref: str,
    fetch_refspec: str | None,
    expected_base_sha: str | None,
) -> str:
    """Fetch the checkout ref if needed, then return the worktree start point.

    An omitted pin keeps the remote-tracking ref. A pin must match the one
    fully qualified commit before ``worktree add`` runs.
    """
    from awf.node.git_manager import GitOperationError

    try:
        if fetch_refspec is not None:
            await run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "fetch",
                    "origin",
                    fetch_refspec,
                ],
                operation="mirror.fetch_checkout_ref",
            )
        if expected_base_sha is None:
            await run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "rev-parse",
                    "--verify",
                    tracking_ref,
                ],
                operation="mirror.rev-parse",
            )
            return tracking_ref
        return await verified_expected_base_oid(
            run,
            mirror_path=mirror_path,
            base_branch=base_branch,
            expected_base_sha=expected_base_sha,
        )
    except GitOperationError as exc:
        if expected_base_sha is not None and exc.reason_code in {
            "GIT_BASE_BRANCH_MISSING",
            "GIT_EXPECTED_BASE_SHA_MISMATCH",
            "GIT_EXPECTED_BASE_SHA_AMBIGUOUS",
        }:
            raise
        raise GitOperationError(
            operation="worktree.add",
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
            reason_code="GIT_BASE_BRANCH_MISSING",
        ) from exc


async def verified_expected_base_oid(
    run: _GitRunner,
    *,
    mirror_path: Path,
    base_branch: str,
    expected_base_sha: str,
) -> str:
    """Resolve only the named remote-tracking commit and require the pin.

    The exact ``refs/remotes/origin/...`` ref must exist before commit peeling.
    Revision syntax such as ``branch~1`` is not that ref and fails closed as a
    missing branch. The rev-parse then uses ``^{commit}`` so a tag or another
    namespace with the same short name cannot win.
    """
    from awf.node.git_manager import GitOperationError

    tracking_ref, _fetch_refspec = _git_manager_mirror._checkout_tracking_ref(base_branch)
    if tracking_ref.startswith("refs/"):
        ref = tracking_ref
    else:
        ref = f"refs/remotes/origin/{tracking_ref.removeprefix('origin/')}"
    qualified = f"{ref}^{{commit}}"
    expected = expected_base_sha.strip().lower()
    try:
        await run(
            [
                "git",
                "--git-dir",
                str(mirror_path),
                "show-ref",
                "--verify",
                "--quiet",
                "--",
                ref,
            ],
            operation="mirror.expected_base_ref",
        )
        result = await run(
            [
                "git",
                "--git-dir",
                str(mirror_path),
                "rev-parse",
                "--verify",
                "--end-of-options",
                qualified,
            ],
            operation="mirror.expected_base_sha",
        )
    except GitOperationError as exc:
        if "ambiguous" in exc.stderr.lower():
            raise GitOperationError(
                operation="worktree.add",
                returncode=exc.returncode,
                stdout=exc.stdout,
                stderr=exc.stderr,
                reason_code="GIT_EXPECTED_BASE_SHA_AMBIGUOUS",
            ) from exc
        if exc.reason_code in {
            "GIT_EXPECTED_BASE_SHA_MISMATCH",
            "GIT_EXPECTED_BASE_SHA_AMBIGUOUS",
            "GIT_BASE_BRANCH_MISSING",
        }:
            raise
        raise GitOperationError(
            operation="worktree.add",
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
            reason_code="GIT_BASE_BRANCH_MISSING",
        ) from exc
    stdout = str(result.stdout)
    stderr = str(result.stderr)
    lines = [line.strip().lower() for line in stdout.splitlines() if line.strip()]
    if (
        len(lines) != 1
        or re.fullmatch(r"[0-9a-f]{40}", lines[0]) is None
        or "ambiguous" in stderr.lower()
    ):
        raise GitOperationError(
            operation="worktree.add",
            returncode=int(result.returncode),
            stdout=stdout,
            stderr=stderr,
            reason_code="GIT_EXPECTED_BASE_SHA_AMBIGUOUS",
        )
    resolved = lines[0]
    if resolved != expected:
        raise GitOperationError(
            operation="worktree.add",
            returncode=1,
            stdout="",
            stderr=f"remote-tracking branch resolved to {resolved}, expected {expected}",
            reason_code="GIT_EXPECTED_BASE_SHA_MISMATCH",
        )
    return resolved
