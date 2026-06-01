"""Provider-neutral code-forge seam (issue #345 Phase 1).

This module is the "make the change easy" refactor for BitBucket support: it
extracts a structural ``ForgeClient`` Protocol from the existing ``GitHubClient``
plus a ``make_forge_client`` factory and forge **detection**, so Phase 2 can drop
in a ``BitBucketClient`` without touching any consumer. GitHub stays the only
implementation. A BitBucket repo is *detected* and then *fails fast* with a
reason-coded :class:`ForgeNotSupportedError` — never a crash, never a silent
mis-route to GitHub.

Resolution + dispatch flow::

    repo_url ──► RepoRef.from_url ──► host? ──► forge (github|bitbucket)   [URL layer]
    workspace.yml forge: (auto|github|bitbucket) ──┐
                                                   ▼
    profile resolver: explicit forge  >  URL host  >  default github       [precedence]
                                                   ▼
                                  resolved_profile.forge   (persisted once)
                                                   ▼
            make_forge_client(forge, runner) ──► github: GitHubClient
                                              └─► bitbucket: raise FORGE_NOT_SUPPORTED (Phase 1)
                                                   ▼
            consumers depend on ForgeClient (Protocol), not GitHubClient

The ``ForgeClient`` Protocol surface is derived by hand from the public
``GitHubClient`` methods — **keep the two in lockstep**. ``GitHubClient`` must
satisfy the Protocol *structurally*, with no inheritance change.

``BranchOpenPullRequestResolver`` is intentionally NOT on this Protocol. It is a
distinct collaborator with a different surface (``resolve(repo_url, branch_name,
base_branch)``), wired separately into ``ControlWorker``, and a BitBucket
workspace fails fast at the executor forge gate before any open-PR-resolver path
matters. A forge-neutral open-PR resolver is deferred to Phase 2.

Import direction (no cycles at module load)::

    db.enums (leaf)             ← github_client      ← forge
    monitor_state_keys (leaf)   ← runtime.pr_monitor ← forge   (CheckFailure, PRStatus)

``forge`` depends on ``runtime.pr_monitor`` only for the neutral ``CheckFailure``
and ``PRStatus`` types. That edge does *not* close a cycle: ``runtime.pr_monitor``
imports only ``runtime.monitor_state_keys`` (a leaf) — it does **not** import
``runtime.pr_monitor_runner``, which is the package that imports ``forge`` back.
So the ``pr_monitor_runner → forge`` back-edge never loops through ``pr_monitor``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast, runtime_checkable

from awf.common.commands import AsyncCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
from awf.db.enums import ForgeKind
from awf.runtime.pr_monitor import CheckFailure, PRStatus

FORGE_NOT_SUPPORTED_REASON_CODE = "FORGE_NOT_SUPPORTED"

_SUPPORTED_FORGES: frozenset[ForgeKind] = frozenset({"github"})


class ForgeNotSupportedError(Exception):
    """A detected forge has no implementation yet (Phase 1: BitBucket).

    Carries a stable ``reason_code`` so the failure flows end-to-end
    (exception → log field → ``WorkspaceEvent`` → policy) like every other
    reason-coded failure. Catch this specifically, never bare ``Exception``.
    """

    def __init__(
        self,
        *,
        message: str,
        reason_code: str = FORGE_NOT_SUPPORTED_REASON_CODE,
    ) -> None:
        """Store the operator-facing message and stable reason code."""
        self.message = message
        self.reason_code = reason_code
        super().__init__(message)


@runtime_checkable
class ForgeClient(Protocol):
    """Provider-neutral interface over a code forge's PR/CI/merge operations.

    Structural mirror of the public ``GitHubClient`` async methods (the source
    of truth — keep in lockstep). Request/response types stay provider-neutral
    (``RepoRef``, ``PRStatus``, ``CheckFailure``) so a future ``BitBucketClient``
    satisfies the same surface.
    """

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        """Return the fully assembled PR status snapshot."""
        ...

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        log_tail_chars: int = 3000,
        pytest_fallback_commands: Sequence[str] = (),
    ) -> tuple[CheckFailure, ...]:
        """Return logs for failing/timed-out checks at ``head_sha``."""
        ...

    async def rerun_failed_workflow_jobs(self, *, repo: RepoRef, run_id: str) -> None:
        """Rerun only the failed jobs for a workflow run."""
        ...

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Resolve a review thread by node ID."""
        ...

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Post a top-level PR comment."""
        ...

    async def create_issue(self, *, repo: RepoRef, title: str, body: str) -> str:
        """Open a tracking issue and return its URL."""
        ...

    async def create_pull_request(
        self,
        *,
        repo: RepoRef,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR for ``head`` against ``base`` and return its URL."""
        ...

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return repository-level enabled merge methods."""
        ...

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,
    ) -> tuple[str, ...] | None:
        """Return base-branch pull-request ruleset merge methods (or ``None``)."""
        ...

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Merge a PR and return the merge commit SHA."""
        ...


def _forge_not_supported_error(forge: object) -> ForgeNotSupportedError:
    """Build the honest fail-fast error for an unsupported/unknown forge."""
    if forge == "bitbucket":
        message = (
            "BitBucket forge support is not yet implemented "
            "(issue #345 Phase 1 adds detection only)."
        )
    else:
        message = f"Unsupported forge {forge!r}: only 'github' is implemented (issue #345 Phase 1)."
    return ForgeNotSupportedError(message=message)


def ensure_forge_supported(forge: ForgeKind) -> None:
    """Fail fast for a forge AWF cannot yet act on.

    Single source of truth for "which forges are supported" — both
    :func:`make_forge_client` and the executor's early forge gate route through
    here so detection and dispatch cannot drift.
    """
    if forge not in _SUPPORTED_FORGES:
        raise _forge_not_supported_error(forge)


def concrete_forge(forge: object) -> ForgeKind:
    """Normalize a persisted/profile forge value to a concrete kind for construction.

    ``None`` / ``""`` / ``"auto"`` → ``"github"``. Legacy ``resolved_profile``
    snapshots predate the ``forge`` field, so reconstructing a ``WorkspaceProfile``
    from them yields the schema default ``"auto"``; those workspaces are all
    GitHub, so they must construct a ``GitHubClient`` rather than fail. Concrete
    values (including unknown ones) pass through unchanged so
    :func:`make_forge_client` still fails closed on anything unsupported.
    """
    if forge in (None, "", "auto"):
        return "github"
    return cast("ForgeKind", forge)


def concrete_forge_for_repo(forge: object, repo_url: str | None) -> ForgeKind:
    """Resolve a gate forge: persisted concrete value > repo-URL host > github.

    Like :func:`concrete_forge`, but when the persisted value is *non-concrete* —
    ``None`` from a **missing** ``resolved_profile`` snapshot, or ``"auto"`` from a
    **legacy** snapshot that predates the ``forge`` field — it detects the forge
    from ``repo_url`` before falling back to github. This mirrors the profile
    resolver's precedence (explicit forge > URL host > github), so the executor
    forge gate trips ``FORGE_NOT_SUPPORTED`` for a BitBucket repo whose snapshot
    omits a concrete forge, instead of silently defaulting to github and
    mis-routing into the ``gh`` path the snapshot-less executor resolves later.
    A concrete persisted value always wins (the resolver already decided it at
    provision time) and undetectable URLs fall through to github via
    :func:`concrete_forge`, so detection stays best-effort.
    """
    if forge in (None, "", "auto"):
        detected = detect_forge_from_url(repo_url) if repo_url else None
        if detected is not None:
            return detected
    return concrete_forge(forge)


def make_forge_client(forge: ForgeKind, runner: AsyncCommandRunner) -> ForgeClient:
    """Return the concrete forge client for ``forge``.

    ``github`` → :class:`GitHubClient`. ``bitbucket`` (or any unknown value that
    slips past typing at runtime) → :class:`ForgeNotSupportedError`, so the
    factory fails closed rather than mis-routing to GitHub.
    """
    ensure_forge_supported(forge)
    return GitHubClient(runner)


def detect_forge_from_url(repo_url: str) -> ForgeKind | None:
    """Best-effort forge detection from a repository URL.

    Returns the detected :data:`~awf.db.enums.ForgeKind`, or ``None`` for an
    unknown host or a malformed URL. Detection is best-effort: a ``None`` result
    lets the profile resolver fall through to the default forge — the executor
    forge gate is the real fail-fast point.
    """
    try:
        return RepoRef.from_url(repo_url).forge
    except ValueError:
        return None
