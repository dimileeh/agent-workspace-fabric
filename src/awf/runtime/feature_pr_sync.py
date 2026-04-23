"""Feature-PR sync core — resolve metadata for attaching a monitor to an
already-open feature PR.

Counterpart to ``release_pr_sync.py``, but for the much more common
case: a feature branch → development (or any base) PR that already
exists and needs a monitor attached.

Two canonical launch scenarios:

1. **Failed-workspace recovery.** An AWF workspace opened a PR, then
   its own validation hiccuped (harness bug, transient infra) before
   the state machine reached ``monitoring_pr``. The feature branch is
   still good; the human just needs an easy way to stick the monitor
   onto the existing PR.

2. **External PR.** A human or another tool opened a PR. We want to
   run the same comment-addressing / CI-watching / (optional) merge
   automation over it without having to re-generate the code inside
   AWF.

The CLI at ``scripts/attach_feature_pr_monitor.py`` consumes this
module: it looks up the PR's head branch, refuses closed/merged PRs
up front (so we don't waste a workspace on something that can't
transition), and hands the metadata to ``run_awf.py`` as a
``sync_feature_pr`` task spec.

All subprocess work routes through an ``AsyncCommandRunner`` —
``FakeCommandRunner`` drives the tests. No direct ``subprocess`` /
``requests`` calls in this module.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from awf.common.commands import AsyncCommandRunner
from awf.common.github_client import RepoRef
from awf.common.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class FeaturePRMetadata:
    """Everything the CLI needs to provision a sync-feature-PR workspace.

    Intentionally distinct from ``PRStatus`` (which is what the monitor
    loop polls against GitHub): this is the one-shot "static" view
    required to *start* the workspace — branch names, URL, author —
    rather than the constantly-mutating review / CI state.
    """

    number: int
    head_branch: str
    """The branch that holds the PR's commits — gets checked out into the
    workspace's worktree so fix commits push back to it."""

    base_branch: str
    """The target of the PR (e.g. ``development``). Used as
    ``branch_base`` in the task spec for SyncBase merges."""

    state: str
    """GitHub's ``state`` enum (``OPEN`` / ``CLOSED`` / ``MERGED``).
    We only return when state == OPEN — other states raise."""

    is_draft: bool
    closed: bool
    merged: bool
    author: str | None
    url: str
    title: str


class FeaturePRSyncError(Exception):
    """Raised when the lookup can't complete (gh error, closed PR, bad payload)."""

    def __init__(self, *, operation: str, stderr: str) -> None:
        self.operation = operation
        self.stderr = stderr
        super().__init__(f"{operation} failed: {stderr.strip() or '<no output>'}")


# The set of fields we pull from ``gh pr view``. Keep in sync with
# ``_parse_metadata`` — adding a field here without a parser update is
# a silent drop on the floor.
_PR_VIEW_JSON_FIELDS = "number,headRefName,baseRefName,state,isDraft,closed,merged,author,url,title"


async def fetch_pr_metadata(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    pr_number: int,
) -> FeaturePRMetadata:
    """Look up a PR via ``gh pr view`` and return the attach-ready metadata.

    Raises ``FeaturePRSyncError`` when:
      - ``gh`` exits non-zero (PR missing, auth issue, gh not installed).
      - The JSON payload can't be parsed (schema drift).
      - A required field is missing (e.g. ``headRefName`` unset).
      - The PR is closed or merged — attaching a monitor would spin
        forever against a terminal PR.
    """
    result = await runner.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo.slug(),
            "--json",
            _PR_VIEW_JSON_FIELDS,
        ]
    )
    if result.returncode != 0:
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr=result.stderr or f"gh pr view exited {result.returncode}",
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr=f"failed to parse gh pr view JSON: {exc}",
        ) from exc

    return _parse_metadata(payload)


def _parse_metadata(payload: dict) -> FeaturePRMetadata:
    """Convert the raw gh JSON into a ``FeaturePRMetadata``. Refuses
    terminal PRs (closed/merged) with a clear error so the CLI can
    exit 1 without spawning a workspace."""
    try:
        number = int(payload["number"])
        head_branch = payload["headRefName"]
        base_branch = payload["baseRefName"]
        state = payload["state"]
        is_draft = bool(payload.get("isDraft", False))
        closed = bool(payload.get("closed", False))
        merged = bool(payload.get("merged", False))
        url = payload["url"]
        title = payload.get("title", "")
    except (KeyError, TypeError, ValueError) as exc:
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr=(
                f"gh pr view payload missing required field or wrong type (head/base/number): {exc}"
            ),
        ) from exc

    if not head_branch:
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr="PR has no headRefName — cannot check out a branch",
        )
    if not base_branch:
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr="PR has no baseRefName — cannot compute branch_base",
        )

    if merged:
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr=f"PR #{number} is already merged — no monitor to attach",
        )
    if closed or state == "CLOSED":
        raise FeaturePRSyncError(
            operation="fetch_pr_metadata",
            stderr=f"PR #{number} is closed — no monitor to attach",
        )

    author_obj = payload.get("author")
    author = author_obj.get("login") if isinstance(author_obj, dict) else None

    return FeaturePRMetadata(
        number=number,
        head_branch=head_branch,
        base_branch=base_branch,
        state=state,
        is_draft=is_draft,
        closed=closed,
        merged=merged,
        author=author,
        url=url,
        title=title,
    )


def build_sync_feature_pr_task_spec(
    *,
    repo_url: str,
    metadata: FeaturePRMetadata,
    agent: str,
    auto_merge: bool,
    companions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the task-spec dict consumed by ``run_awf.py`` with
    ``task_kind="sync_feature_pr"``.

    Pure function — no I/O. Keeps spec generation deterministic so
    the CLI can safely write the result to a deterministic path for
    idempotency.

    ``auto_merge`` is a deliberate per-call decision. The safe default
    at the CLI layer is ``False`` (notify-human terminal) so unexpected
    auto-merges never land; callers opt in explicitly for the
    recover-my-failed-workspace case where auto-merge semantics are
    actually wanted.
    """
    repo = RepoRef.from_url(repo_url)
    return {
        "repo_url": repo_url,
        "branch_base": metadata.base_branch,
        "source_branch": metadata.head_branch,
        "pr_number": metadata.number,
        "task_kind": "sync_feature_pr",
        "task_title": f"feature-pr-monitor: {repo.slug()}#{metadata.number}",
        "task_prompt": _task_prompt(metadata=metadata, repo=repo),
        "agent": agent,
        "auto_merge": auto_merge,
        "test_commands": [],
        "requires_database": False,
        # Defensive copy: callers sometimes reuse the companions list
        # across multiple PR attachments.
        "companions": list(companions) if companions else [],
    }


def _task_prompt(*, metadata: FeaturePRMetadata, repo: RepoRef) -> str:
    """The coding CLI's entry-level context. For ``sync_feature_pr`` the
    feature code already exists on the branch — the job is addressing
    reviewer comments and keeping the base in sync, not re-implementing."""
    return (
        f"AWF sync-feature-pr monitor for {repo.slug()}#{metadata.number} "
        f"(head: {metadata.head_branch} → base: {metadata.base_branch}).\n\n"
        f"No fresh implementation runs on entry. The monitor is attached to "
        f"an already-open PR to drive it through reviewer comments, CI fixes, "
        f"and base-branch sync. The coding CLI is invoked only when the "
        f"monitor surfaces an AddressComments / ReportCiFailure / SyncBase "
        f"action that needs code changes.\n\n"
        f"PR title: {metadata.title}"
    )


def task_spec_filename(*, repo_slug: str, pr_number: int) -> str:
    """Deterministic filename for the spec file in
    ``<work_dir>/feature-pr-specs/``.

    The ``-feature-pr`` discriminator distinguishes these from the
    release-PR schedule specs (``-pr<n>``) so the idempotency pgrep
    doesn't see cross-kind false positives on the same repo+PR
    number (which CAN collide: a release PR #272 and a feature PR
    #272 on the same repo).
    """
    return f"{repo_slug.replace('/', '__')}-feature-pr{pr_number}.json"


def is_feature_pr_monitor_running(
    *,
    spec_filename: str,
    process_lister: Callable[[], str] | None = None,
) -> bool:
    """True iff a ``run_awf.py`` process is currently driving
    ``spec_filename`` — detected by pgrep-ing the process list and
    matching against the spec path.

    ``process_lister`` is a seam for tests; production passes ``None``
    and we shell out to ``pgrep -af run_awf.py`` ourselves. Any
    pgrep failure returns ``False`` — never block a launch on an
    introspection error (pgrep may not even be installed on some hosts).
    """
    if process_lister is None:
        process_lister = _default_process_lister
    try:
        out = process_lister()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return any(spec_filename in line for line in out.splitlines())


def _default_process_lister() -> str:
    try:
        return subprocess.check_output(
            ["pgrep", "-af", "run_awf.py"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        # Exit 1 = "no processes matched" — a perfectly valid empty list.
        return ""
