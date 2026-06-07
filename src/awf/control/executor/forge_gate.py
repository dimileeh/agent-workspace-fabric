"""Forge-support gate for the executor execution flow.

Mechanically extracted from ``execution_flow``; behavior is unchanged. Keeping
the resolved-vs-detected forge reasoning here keeps ``execution_flow`` focused on
the lifecycle and lets the gate be unit-tested as a pure decision.
"""

from __future__ import annotations

from typing import Any

from awf.common.forge import (
    PR_CREATE_FORGE_NOT_SUPPORTED_REASON_CODE,
    ForgeNotSupportedError,
    concrete_forge_for_repo,
    ensure_forge_supported,
)


def unsupported_forge_error(ws: Any) -> ForgeNotSupportedError | None:
    """Return a ``ForgeNotSupportedError`` when ``ws``'s resolved forge is unsupported.

    This is the earliest point the resolved forge is known, and runs BEFORE every
    downstream gh path: the non-feature dispatch (sync_release_pr / sync_feature_pr
    build ``gh`` in monitor handoff), the agent run, push, and ``gh pr create``. A
    detected-but-unimplemented forge (e.g. bitbucket) must fail fast here with
    FORGE_NOT_SUPPORTED rather than strand the workspace on an uncaught error
    deeper in a handoff.

    Prefer the persisted ``resolved_profile`` (reconstructed, never re-resolved);
    but when the snapshot is *missing* (None) or *legacy* (forge reconstructs as
    ``auto``) — a path the executor still supports, since it resolves+persists a
    profile from ``ws.repo_url`` before running — detect the forge from the repo
    URL so a BitBucket repo trips the gate instead of defaulting to github and
    slipping into the gh path. A concrete persisted forge always wins; undetectable
    URLs fall back to github so pre-existing GitHub workspaces never trip the gate.

    URL detection is best-effort, so a missing/legacy snapshot whose repo_url
    detects as github (an explicit ``forge: bitbucket`` lives only in the
    requested/repo-local profile, or a bare ``owner/repo`` slug) passes this
    pre-resolution call. The executor closes that gap by re-invoking this gate
    on ``ws`` immediately after it resolves+persists the snapshot, when
    ``ws.resolved_profile`` now carries the concrete forge — so the explicit
    forge fails fast before the agent run / push / ``gh pr create``.

    Returns ``None`` when the forge is supported so the caller proceeds.
    """
    try:
        ensure_forge_supported(
            concrete_forge_for_repo((ws.resolved_profile or {}).get("forge"), ws.repo_url)
        )
    except ForgeNotSupportedError as exc:
        return exc
    return None


def new_pr_unsupported_forge_error(ws: Any) -> ForgeNotSupportedError | None:
    """Legacy new-PR forge gate — no longer wired into the execution flow.

    AWF once shelled ``gh pr create`` (GitHub-only, ``github.com`` URLs only) from
    ``PullRequestCreator.push_and_open`` to open new PRs, so a BitBucket feature
    workspace *without* an existing PR (``ws.pr_url`` unset) had to fail fast here
    with ``PR_CREATE_FORGE_NOT_SUPPORTED`` rather than run the agent and validation
    and then mis-route to ``gh``/github.com at PR creation. That is no longer true:
    new-PR creation is forge-neutral now — ``push_and_open`` opens the PR through
    the injected :class:`~awf.common.forge.ForgeClient` (GitHub or BitBucket), so a
    BitBucket workspace creates its PR directly and the executor no longer calls
    this gate. The function and its ``PR_CREATE_FORGE_NOT_SUPPORTED`` reason code are
    retained as dead code (with their tests) pending a follow-up cleanup.

    Returns ``None`` for a GitHub forge (or an undetectable URL that falls back to
    github) and for a workspace that already has a PR (``ws.pr_url`` set); otherwise
    returns the ``PR_CREATE_FORGE_NOT_SUPPORTED`` error.
    """
    if ws.pr_url:
        return None
    forge = concrete_forge_for_repo((ws.resolved_profile or {}).get("forge"), ws.repo_url)
    if forge == "github":
        return None
    return ForgeNotSupportedError(
        message=(
            f"Opening a new pull request on forge {forge!r} is not supported: AWF "
            "shells `gh pr create` (GitHub-only) to open new PRs. The forge is "
            "supported for monitoring an existing PR, but new-PR creation is not "
            "forge-neutral yet — open the pull request manually first, or use a "
            "GitHub repository."
        ),
        reason_code=PR_CREATE_FORGE_NOT_SUPPORTED_REASON_CODE,
    )
