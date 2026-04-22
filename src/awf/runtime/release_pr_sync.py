"""Release-PR sync core — idempotent development→main PR maintenance.

This is the Phase 2 companion to ``release_pr_monitor.py``. The monitor
drives a PR through comments + CI + base-sync + (notify-human). This
module's job is narrower: given a repo + source branch + target branch,

    1. Check whether source has commits not on target (``rev-list --count``).
       If not, exit no-op.
    2. Check whether an open PR already exists between those refs
       (``gh pr list --state open --base <target> --head <source>``).
       If yes, return its number — the monitor (if active) will pick
       up new commits on its next poll via SyncBase.
    3. Otherwise, open a PR (``gh pr create``) with an auto-generated
       body listing feature PRs merged into the source branch since the
       last release PR.

The function is pure-ish: all side effects route through an
``AsyncCommandRunner`` so ``FakeCommandRunner`` drives tests.

Callers:

* **`scripts/schedule_release_pr.py`** — one-shot CLI. Runs this
  function; if a PR is open (either pre-existing or freshly created),
  also spins up a ``sync_release_pr`` workspace with the monitor
  attached. Intended to be invoked by a cron / GitHub webhook /
  ``release_pr_watcher.py`` when development advances.
* **`scripts/release_pr_watcher.py`** — long-running daemon. Polls
  each configured repo, invokes the scheduler when dev is ahead of
  main. (GitHub webhooks can replace the poller later when AWF has a
  public HTTP surface; the functional contract is identical.)

None of this runs inside a container — everything happens on the
host via ``gh`` CLI and local git. The container + worktree only
come into play once the release-PR monitor handoff happens (comment
resolution invokes the coding CLI in the agent container).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from awf.common.commands import AsyncCommandRunner
from awf.common.github_client import RepoRef
from awf.common.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class ReleasePrResult:
    """What ``ensure_release_pr_open`` produces.

    * ``pr_number`` is set whenever a PR exists (pre-existing or newly
      opened). ``None`` means the repo is in sync — no-op.
    * ``created`` is ``True`` only for the fresh-open path. Callers use
      this to decide whether to emit a "new release PR" notification.
    """

    pr_number: int | None
    pr_url: str | None
    ahead_by: int
    created: bool
    reason: str  # human-readable, goes into structured logs / CLI output


class ReleasePrSyncError(Exception):
    """Raised when the sync can't complete (gh CLI error, bad repo, etc.)."""

    def __init__(self, *, operation: str, stderr: str) -> None:
        self.operation = operation
        self.stderr = stderr
        super().__init__(f"{operation} failed: {stderr.strip() or '<no output>'}")


async def ensure_release_pr_open(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    source_branch: str = "development",
    target_branch: str = "main",
    dry_run: bool = False,
) -> ReleasePrResult:
    """Idempotently ensure a release PR exists if the source diverges from target.

    Order of operations:

    1. Count commits on source not on target via remote refs
       (``gh api repos/<owner>/<repo>/compare/<target>...<source>``).
       Zero → no-op.
    2. Query existing open PRs with that base/head pair. Found → return.
    3. Create PR with an auto-generated title + body.
    """
    ahead = await _count_commits_ahead(
        runner=runner, repo=repo, source=source_branch, target=target_branch
    )
    if ahead == 0:
        return ReleasePrResult(
            pr_number=None,
            pr_url=None,
            ahead_by=0,
            created=False,
            reason="source is at or behind target; no release needed",
        )

    existing = await _find_open_release_pr(
        runner=runner, repo=repo, source=source_branch, target=target_branch
    )
    if existing is not None:
        number, url = existing
        return ReleasePrResult(
            pr_number=number,
            pr_url=url,
            ahead_by=ahead,
            created=False,
            reason=(
                f"release PR already open ({url}); monitor will SyncBase "
                "on its next poll"
            ),
        )

    if dry_run:
        return ReleasePrResult(
            pr_number=None,
            pr_url=None,
            ahead_by=ahead,
            created=False,
            reason=f"dry-run: would open a release PR for {ahead} commit(s) ahead",
        )

    title, body = await _build_release_pr_body(
        runner=runner, repo=repo, source=source_branch, target=target_branch, ahead=ahead
    )
    created_number, created_url = await _create_pr(
        runner=runner,
        repo=repo,
        source=source_branch,
        target=target_branch,
        title=title,
        body=body,
    )
    return ReleasePrResult(
        pr_number=created_number,
        pr_url=created_url,
        ahead_by=ahead,
        created=True,
        reason=f"opened release PR {created_url} with {ahead} commit(s)",
    )


# ── Internals ──────────────────────────────────────────────────────────────


async def _count_commits_ahead(
    *, runner: AsyncCommandRunner, repo: RepoRef, source: str, target: str
) -> int:
    """Number of commits on ``source`` that aren't on ``target``.

    Uses GitHub's compare API rather than a local git clone so the
    watcher/scheduler don't need a persistent local mirror. ``gh api``
    authenticates via the existing gh credential.
    """
    result = await runner.run(
        [
            "gh",
            "api",
            f"repos/{repo.slug()}/compare/{target}...{source}",
            "--jq",
            ".ahead_by",
        ]
    )
    if not result.ok:
        raise ReleasePrSyncError(
            operation=f"gh api compare {target}...{source}", stderr=result.stderr
        )
    text = (result.stdout or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise ReleasePrSyncError(
            operation="parse ahead_by", stderr=f"{exc}: got {text!r}"
        ) from exc


async def _find_open_release_pr(
    *, runner: AsyncCommandRunner, repo: RepoRef, source: str, target: str
) -> tuple[int, str] | None:
    """Return ``(number, url)`` of an open PR from source → target, if any.

    ``gh pr list`` filters on both ``--base`` and ``--head``; we trust
    it to uniquely identify the release PR because (a) we only ever
    open one at a time, (b) GitHub itself refuses to open a duplicate
    PR with identical base+head."""
    result = await runner.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo.slug(),
            "--state",
            "open",
            "--base",
            target,
            "--head",
            source,
            "--json",
            "number,url",
        ]
    )
    if not result.ok:
        raise ReleasePrSyncError(operation="gh pr list", stderr=result.stderr)
    payload: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    if not payload:
        return None
    # Take the first (there should only ever be one).
    row = payload[0]
    return int(row["number"]), str(row["url"])


async def _build_release_pr_body(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    source: str,
    target: str,
    ahead: int,
) -> tuple[str, str]:
    """Produce (title, body) for the new release PR.

    Title: ``release: <source> → <target> (<N> commit(s))``.
    Body: an enumeration of merged feature PRs in the range, plus a
    pointer to AWF's monitor. We fetch the commit list via the same
    compare API used for ahead_by — single extra round trip.
    """
    title = f"release: {source} → {target} ({ahead} commit{'s' if ahead != 1 else ''})"
    commits_result = await runner.run(
        [
            "gh",
            "api",
            f"repos/{repo.slug()}/compare/{target}...{source}",
            "--jq",
            "[.commits[] | {sha: .sha[0:7], message: (.commit.message | split(\"\\n\")[0])}]",
        ]
    )
    if not commits_result.ok:
        # Body fetch is best-effort; degrade gracefully.
        commit_lines = "(commit list unavailable)"
    else:
        try:
            commits: list[dict[str, str]] = json.loads(commits_result.stdout or "[]")
            bullets = [f"- `{c['sha']}` {c['message']}" for c in commits[:50]]
            if len(commits) > 50:
                bullets.append(f"- …and {len(commits) - 50} more.")
            commit_lines = "\n".join(bullets) or "(no commits listed)"
        except (json.JSONDecodeError, KeyError):
            commit_lines = "(commit list parse failed)"

    body = (
        f"Automated release PR opened by AWF.\n\n"
        f"**Source**: `{source}` — **Target**: `{target}` — "
        f"**Ahead by**: {ahead} commit(s).\n\n"
        f"### Commits included\n\n{commit_lines}\n\n"
        f"---\n"
        f"AWF's release-PR monitor will keep this PR up to date as more "
        f"feature branches merge into `{source}`, resolve review threads "
        f"via the coding CLI inside the workspace container, and post a "
        f"\"ready to merge\" comment when all 5 gates are green. "
        f"**This PR will NOT auto-merge** — a human decides when to click "
        f"the merge button."
    )
    return title, body


async def _create_pr(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    source: str,
    target: str,
    title: str,
    body: str,
) -> tuple[int, str]:
    """Open the PR via ``gh pr create`` and return (number, url)."""
    result = await runner.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo.slug(),
            "--base",
            target,
            "--head",
            source,
            "--title",
            title,
            "--body",
            body,
        ]
    )
    if not result.ok:
        raise ReleasePrSyncError(operation="gh pr create", stderr=result.stderr)
    # `gh pr create` emits the new PR URL on stdout. Parse the PR number
    # out of it.
    url = (result.stdout or "").strip().splitlines()[-1].strip()
    try:
        number = int(url.rsplit("/pull/", 1)[1].split("/")[0])
    except (IndexError, ValueError) as exc:
        raise ReleasePrSyncError(
            operation="gh pr create (parse url)",
            stderr=f"{exc}: unexpected output {url!r}",
        ) from exc
    return number, url
