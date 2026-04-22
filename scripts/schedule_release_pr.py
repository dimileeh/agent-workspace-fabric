"""One-shot CLI: ensure a development→main release PR is open for a repo.

This is the "webhook endpoint" equivalent for MVP. Invoke it whenever
you want to check a repo for divergence and open/reuse a release PR.

Callers:

* ``scripts/release_pr_watcher.py`` — invokes this on a 60-second poll
  cycle (the substitute for real GitHub webhooks in the MVP).
* A future GitHub webhook receiver could shell out to this script
  once AWF has a public HTTP surface.
* Humans can run it manually:
  ``./scripts/schedule_release_pr.py --repo git@github.com:dimileeh/aira-agent.git``

Behaviour:

1. Check the source branch for commits ahead of target. If zero, exit 0.
2. Check for an existing open release PR. If one exists, exit 0
   (AWF's release-PR monitor — if running — keeps it current).
3. Otherwise, open a fresh release PR via gh CLI.

**Does NOT** spin up an AWF workspace to monitor the PR. That's a
separate concern — the watcher (or a cron) invokes ``run_awf.py`` with
a ``sync_release_pr`` task spec when it decides a monitor should be
attached. See ``docs/PLAN_RELEASE_PR_SYNC.md`` for the full flow.

Exit codes:
  0 — success (PR opened, reused, or no divergence).
  1 — sync error (gh CLI failed, bad repo, network, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from awf.common.commands import AsyncioSubprocessRunner  # noqa: E402
from awf.common.github_client import RepoRef  # noqa: E402
from awf.runtime.release_pr_sync import (  # noqa: E402
    ReleasePrSyncError,
    ensure_release_pr_open,
)


async def _main(
    *,
    repo_url: str,
    source_branch: str,
    target_branch: str,
    dry_run: bool,
) -> int:
    runner = AsyncioSubprocessRunner()
    repo = RepoRef.from_url(repo_url)
    try:
        result = await ensure_release_pr_open(
            runner=runner,
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            dry_run=dry_run,
        )
    except ReleasePrSyncError as exc:
        print(f"release-pr-sync ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"[{repo.slug()}] ahead_by={result.ahead_by} "
        f"pr_number={result.pr_number} created={result.created}"
    )
    print(f"  {result.reason}")
    if result.pr_url:
        print(f"  URL: {result.pr_url}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        required=True,
        help='Repo URL or slug, e.g. "git@github.com:dimileeh/aira-agent.git" '
        'or "dimileeh/aira-agent".',
    )
    parser.add_argument("--source", default="development", help="Source branch (default: development)")
    parser.add_argument("--target", default="main", help="Target branch (default: main)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without opening a PR.",
    )
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            _main(
                repo_url=args.repo,
                source_branch=args.source,
                target_branch=args.target,
                dry_run=args.dry_run,
            )
        )
    )
