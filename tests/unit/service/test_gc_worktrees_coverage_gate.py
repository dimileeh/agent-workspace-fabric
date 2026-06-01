"""Coverage-gate unit tests for :mod:`awf.service.gc_worktrees`.

These tests close the combined line+branch coverage gaps that remained in
``awf.service.gc_worktrees`` after the original ``test_gc_worktree_remover.py``
suite:

* ``blocked_worktree_paths_after_remove`` lines 79-81 / branch 80: an
  *unreported* worktree id (present on the candidate's path map but absent from
  the remove result's ``target_results``) must be treated as still-blocked and
  added to the returned set.
* ``default_worktree_remover`` lines 132-140 / branch 132: a companion worktree
  whose on-disk path exists but is *not* git-managed must be recorded as a
  ``skipped`` / ``WORKTREE_NOT_GIT_MANAGED`` target and skipped via ``continue``
  rather than handed to git for removal.

All tests are pure unit tests: no real database, no Docker, no network, no
sleeping. The async ``default_worktree_remover`` is exercised with a fake async
session factory, an in-memory ``Workspace`` stand-in, a monkeypatched
``companion_worktree_remove_targets`` (so the test does not depend on companion
schema details), a stubbed ``GitManager`` (so no real git runs), and real
directories under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from awf.service import gc_worktrees
from awf.service.gc_worktrees import (
    WorkspaceGCWorktreeRemoveResult,
    WorkspaceGCWorktreeRemoveTargetResult,
    blocked_worktree_paths_after_remove,
    default_worktree_remover,
    is_existing_non_git_worktree,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Lightweight duck-typed fakes.
#
# The production functions only read attributes, so minimal stand-ins exercise
# the real code paths without depending on the concrete (and heavier)
# WorkspaceGCCandidate / Workspace constructors.
# ---------------------------------------------------------------------------


@dataclass
class _FakeGCPath:
    path: Path
    exists: bool = True


@dataclass
class _FakeWorktree:
    path: Path
    exists: bool = True


@dataclass
class _FakeCandidate:
    workspace_id: str
    worktree: _FakeWorktree
    companion_worktrees: list[_FakeGCPath] = field(default_factory=list)


@dataclass
class _FakeWorkspace:
    repo_url: str | None


class _FakeSession:
    """Minimal async-context-manager session exposing ``get``."""

    def __init__(self, workspace: _FakeWorkspace | None) -> None:
        self._workspace = workspace

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, _model: object, _pk: str) -> _FakeWorkspace | None:
        return self._workspace


def _session_factory_for(workspace: _FakeWorkspace | None):
    def _factory() -> _FakeSession:
        return _FakeSession(workspace)

    return _factory


class _StubGitManager:
    """Records remove_worktree calls without touching git."""

    def __init__(self, _git_dir: Path) -> None:
        self.removed: list[tuple[str, str]] = []

    async def remove_worktree(self, *, workspace_id: str, repo_url: str) -> None:
        self.removed.append((workspace_id, repo_url))


# ---------------------------------------------------------------------------
# blocked_worktree_paths_after_remove: unreported id branch (lines 79-81).
# ---------------------------------------------------------------------------


def test_blocked_paths_includes_unreported_worktree_id(tmp_path: Path) -> None:
    """An id present on the candidate but missing from results stays blocked.

    Drives the failed-target comprehension (line 77) plus the
    ``for ... if worktree_id not in reported_ids: blocked_paths.add(path)``
    loop (lines 79->80->81): the companion id is reported as ``failed`` while
    the primary workspace id is *not* reported at all, so both paths must end
    up blocked.
    """
    primary_path = tmp_path / "ws-primary"
    companion_path = tmp_path / "companion-abc"
    candidate = _FakeCandidate(
        workspace_id="ws-primary",
        worktree=_FakeWorktree(path=primary_path),
        companion_worktrees=[_FakeGCPath(path=companion_path)],
    )
    # Only the companion is reported (as failed); the primary id is unreported.
    remove_result = WorkspaceGCWorktreeRemoveResult(
        status="partial",
        reason_code="GIT_WORKTREE_REMOVE_FAILED",
        target_results=(
            WorkspaceGCWorktreeRemoveTargetResult(
                worktree_id="companion-abc",
                status="failed",
                reason_code="GIT_WORKTREE_REMOVE_FAILED",
                error="boom",
            ),
        ),
    )

    blocked = blocked_worktree_paths_after_remove(candidate, remove_result)

    # Failed companion (from the comprehension) AND the unreported primary
    # (from the loop) are both blocked.
    assert blocked == {primary_path, companion_path}


def test_blocked_paths_succeeded_target_only_blocks_unreported(
    tmp_path: Path,
) -> None:
    """A succeeded, reported id is released; an unreported id stays blocked.

    Exercises the false side of the ``status == 'failed'`` comprehension filter
    together with the true side of ``worktree_id not in reported_ids`` for the
    companion that has no corresponding target result.
    """
    primary_path = tmp_path / "ws-primary"
    companion_path = tmp_path / "companion-xyz"
    candidate = _FakeCandidate(
        workspace_id="ws-primary",
        worktree=_FakeWorktree(path=primary_path),
        companion_worktrees=[_FakeGCPath(path=companion_path)],
    )
    # Primary succeeded (so not blocked); companion unreported (so blocked).
    remove_result = WorkspaceGCWorktreeRemoveResult(
        status="succeeded",
        reason_code="WORKTREE_REMOVE_SUCCEEDED",
        target_results=(
            WorkspaceGCWorktreeRemoveTargetResult(
                worktree_id="ws-primary",
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            ),
        ),
    )

    blocked = blocked_worktree_paths_after_remove(candidate, remove_result)

    assert blocked == {companion_path}


# ---------------------------------------------------------------------------
# default_worktree_remover: companion is an existing non-git worktree
# (lines 132-140 + the `continue` and branch 132).
# ---------------------------------------------------------------------------


def test_default_remover_skips_non_git_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-git companion dir is reported skipped and never sent to git.

    The primary worktree is git-managed (has ``.git``) so it becomes a real
    removal target, while the companion exists on disk *without* ``.git`` and
    must hit the ``is_existing_non_git_worktree`` skip branch (line 132) that
    appends a ``WORKTREE_NOT_GIT_MANAGED`` target (lines 133-139) and
    ``continue``s (line 140), so it is excluded from ``worktree_targets``.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Primary: existing AND git-managed -> goes to worktree_targets.
    primary_path = tmp_path / "ws-primary"
    primary_path.mkdir()
    (primary_path / ".git").write_text("gitdir", encoding="utf-8")
    assert not is_existing_non_git_worktree(primary_path)

    # Companion: exists on disk but NOT git-managed -> skip branch.
    companion_path = tmp_path / "companion-bare"
    companion_path.mkdir()
    assert is_existing_non_git_worktree(companion_path)

    candidate = _FakeCandidate(
        workspace_id="ws-primary",
        worktree=_FakeWorktree(path=primary_path, exists=True),
        companion_worktrees=[_FakeGCPath(path=companion_path, exists=True)],
    )

    workspace = _FakeWorkspace(repo_url="https://example.com/repo.git")

    # Yield exactly one companion target id matching the companion dir name.
    monkeypatch.setattr(
        gc_worktrees,
        "companion_worktree_remove_targets",
        lambda _ws: [("companion-bare", "https://example.com/companion.git")],
    )
    # Avoid importing/using the real GitManager (and thus real git).
    monkeypatch.setattr("awf.node.git_manager.GitManager", _StubGitManager)

    result = asyncio.run(
        default_worktree_remover(
            candidate,
            session_factory=_session_factory_for(workspace),
            work_dir=work_dir,
        )
    )

    # The companion was skipped (non-git), and only the primary was removed.
    skipped = [t for t in result.target_results if t.worktree_id == "companion-bare"]
    assert len(skipped) == 1
    assert skipped[0].status == "skipped"
    assert skipped[0].reason_code == "WORKTREE_NOT_GIT_MANAGED"

    succeeded = [t for t in result.target_results if t.status == "succeeded"]
    assert [t.worktree_id for t in succeeded] == ["ws-primary"]
    assert result.status == "succeeded"


def test_default_remover_all_targets_non_git_returns_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every target is a non-git existing dir, the whole op is skipped.

    The primary is a non-git existing dir (hits the primary skip branch) and
    the lone companion is also a non-git existing dir (hits lines 132-140),
    leaving ``worktree_targets`` empty so the function returns the aggregate
    ``WORKTREE_NOT_GIT_MANAGED`` skipped result.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    primary_path = tmp_path / "ws-primary"
    primary_path.mkdir()  # exists, no .git -> non-git
    assert is_existing_non_git_worktree(primary_path)

    companion_path = tmp_path / "companion-bare"
    companion_path.mkdir()  # exists, no .git -> non-git
    assert is_existing_non_git_worktree(companion_path)

    candidate = _FakeCandidate(
        workspace_id="ws-primary",
        worktree=_FakeWorktree(path=primary_path, exists=True),
        companion_worktrees=[_FakeGCPath(path=companion_path, exists=True)],
    )
    workspace = _FakeWorkspace(repo_url="https://example.com/repo.git")

    monkeypatch.setattr(
        gc_worktrees,
        "companion_worktree_remove_targets",
        lambda _ws: [("companion-bare", "https://example.com/companion.git")],
    )
    monkeypatch.setattr("awf.node.git_manager.GitManager", _StubGitManager)

    result = asyncio.run(
        default_worktree_remover(
            candidate,
            session_factory=_session_factory_for(workspace),
            work_dir=work_dir,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "WORKTREE_NOT_GIT_MANAGED"
    assert {t.worktree_id for t in result.target_results} == {
        "ws-primary",
        "companion-bare",
    }
    assert all(t.status == "skipped" for t in result.target_results)
