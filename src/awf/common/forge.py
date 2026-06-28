"""Provider-neutral code-forge seam (issue #345 Phase 1).

This module is the "make the change easy" refactor for Bitbucket support: it
extracts a structural ``ForgeClient`` Protocol from the existing ``GitHubClient``
plus a ``make_forge_client`` factory and forge **detection**, so Phase 2 can drop
in a ``BitbucketClient`` without touching any consumer. GitHub stays the only
implementation. A Bitbucket repo is *detected* and then *fails fast* with a
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
                                              └─► bitbucket: BitbucketClient (Phase 2, issue #345)
                                                   ▼
            consumers depend on ForgeClient (Protocol), not GitHubClient

The ``ForgeClient`` Protocol surface is derived by hand from the public
``GitHubClient`` methods — **keep the two in lockstep**. ``GitHubClient`` must
satisfy the Protocol *structurally*, with no inheritance change.

``BranchOpenPullRequestResolver`` is intentionally NOT on this Protocol. It is a
distinct collaborator with a different surface (``resolve(repo_url, branch_name,
base_branch)``), wired separately into ``ControlWorker``, and a Bitbucket
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
from awf.runtime.pr_monitor import CheckFailure, CheckTiming, PRStatus

FORGE_NOT_SUPPORTED_REASON_CODE = "FORGE_NOT_SUPPORTED"
CheckFailureLogResult = tuple[tuple[CheckFailure, ...], bool]

# Distinct from ``FORGE_NOT_SUPPORTED``: the forge itself *is* supported (GitHub or
# Bitbucket Cloud), but the GitHub-only ``BranchOpenPullRequestResolver`` cannot
# recover an open PR for a non-GitHub forge yet (a forge-neutral resolver is
# deferred). Using ``FORGE_NOT_SUPPORTED`` here would tell a bitbucket.org operator
# to switch to a supported forge they are already on — so this path carries its own
# honest reason code instead.
OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED_REASON_CODE = "OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED"

# Distinct again from ``FORGE_NOT_SUPPORTED``: this reason code once fired when a
# Bitbucket feature workspace without an existing PR tried to open one, back when
# opening a brand-new PR shelled ``gh pr create`` via ``PullRequestCreator.push_and_open``
# — a GitHub-only operation that recognized only ``github.com/.../pull/...`` URLs.
# New-PR creation is now forge-neutral via the injected ``ForgeClient`` (Bitbucket
# feature workspaces create their PRs directly), so this reason code is no longer
# raised in production; it and ``new_pr_unsupported_forge_error`` remain as dead
# code pending a follow-up cleanup. Retained here so existing reason-code consumers
# (doctor/reasons, REASON_CATALOG) keep resolving.
PR_CREATE_FORGE_NOT_SUPPORTED_REASON_CODE = "PR_CREATE_FORGE_NOT_SUPPORTED"

_SUPPORTED_FORGES: frozenset[ForgeKind] = frozenset({"github", "bitbucket"})


class ForgeNotSupportedError(Exception):
    """A detected forge has no implementation yet (e.g. a future GitLab forge).

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
    (``RepoRef``, ``PRStatus``, ``CheckFailure``) so a future ``BitbucketClient``
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
        rollup_checks: Sequence[CheckTiming] = (),
    ) -> CheckFailureLogResult:
        """Return failing-check logs and whether relevant runs are still in progress."""
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

    async def aclose(self) -> None:
        """Release any resources the client owns (HTTP pools, sockets).

        :class:`GitHubClient` wraps a stateless subprocess runner and so this is
        a no-op, but :class:`~awf.common.bitbucket_client.BitbucketClient` owns a
        live ``httpx.AsyncClient`` (connection pool + TLS session) that must be
        closed deterministically rather than left to GC. Declaring ``aclose`` on
        the Protocol lets every caller of :func:`make_forge_client` release the
        client uniformly — ``async with make_forge_client(...) as gh:`` — without
        branching on the concrete forge.
        """
        ...

    async def __aenter__(self) -> ForgeClient:
        """Enter an ``async with`` block, returning this client unchanged."""
        ...

    async def __aexit__(self, *exc_info: object) -> None:
        """Release resources on ``async with`` exit (delegates to :meth:`aclose`)."""
        ...


def _forge_not_supported_error(forge: object) -> ForgeNotSupportedError:
    """Build the honest fail-fast error for an unsupported/unknown forge.

    GitHub and Bitbucket are both implemented (issue #345). This fires only for a
    genuinely-unknown forge value (for example ``gitlab``) that slips past typing
    at runtime, so the factory fails closed rather than mis-routing to GitHub.
    """
    message = (
        f"Unsupported forge {forge!r}: only 'github' and 'bitbucket' are implemented (issue #345)."
    )
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
    forge gate trips ``FORGE_NOT_SUPPORTED`` for a Bitbucket repo whose snapshot
    omits a concrete forge, instead of silently defaulting to github and
    mis-routing into the ``gh`` path the snapshot-less executor resolves later.
    A concrete persisted value always wins (the resolver already decided it at
    provision time) and undetectable URLs fall through to github via
    :func:`concrete_forge`, so detection stays best-effort.
    """
    if forge in (None, "", "auto"):
        # Phase 2 caveat: a bare ``owner/repo`` slug carries no host, so
        # ``detect_forge_from_url`` (via ``RepoRef.from_url``) returns ``"github"``
        # for it — a non-concrete forge with a bare-slug ``repo_url`` resolves to
        # github by *detection* here, the same result as the ``concrete_forge``
        # fallback below but reached via the URL path. Always correct in Phase 1
        # (every bare-slug workspace is GitHub); a Phase 2 Bitbucket bare-slug
        # workspace would need a concrete persisted forge, since host detection
        # cannot infer bitbucket from a hostless slug.
        detected = detect_forge_from_url(repo_url) if repo_url else None
        if detected is not None:
            return detected
    return concrete_forge(forge)


def make_forge_client(forge: ForgeKind, runner: AsyncCommandRunner) -> ForgeClient:
    """Return the concrete forge client for ``forge``.

    ``github`` → :class:`GitHubClient` (over ``runner``). ``bitbucket`` →
    :class:`~awf.common.bitbucket_client.BitbucketClient`, built from the
    Bitbucket env contract (``runner`` is unused on this path because the
    Bitbucket client talks HTTP via its own injected ``httpx.AsyncClient``). Any
    unknown value that slips past typing at runtime → :class:`ForgeNotSupportedError`,
    so the factory fails closed rather than mis-routing to GitHub.
    """
    ensure_forge_supported(forge)
    if forge == "bitbucket":
        # Imported lazily so the GitHub-only hot path never pays for httpx import
        # and ``forge`` keeps no import-time dependency on the Bitbucket client.
        from awf.common.bitbucket_client import BitbucketClient

        return BitbucketClient.from_env()
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
