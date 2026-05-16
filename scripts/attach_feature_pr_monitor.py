"""One-shot CLI: attach an AWF PR monitor to an already-open feature PR.

Counterpart to ``scripts/schedule_release_pr.py`` (which covers
development→main release PRs). This script handles the much more
common feature-PR case:

  - A previous AWF workspace opened a PR, then its own run failed
    before reaching the monitor phase (harness bug, flaky infra).
    The feature branch is still good; this CLI attaches the monitor
    to the existing PR without re-running the coding agent.
  - A human or another tool opened a feature PR. This CLI runs the
    same comment-addressing / CI-watching / (optional) merge
    automation against it.

Behaviour:

  1. By default, call the supported AWF API endpoint
     ``POST /v1/workspaces/adopt-pr``. The control plane resolves PR metadata,
     rejects closed/merged PRs, creates or attaches the service-managed
     workspace, and owns monitor idempotency.
  2. The old detached ``run_awf.py`` path remains available only with
     ``--legacy-detached`` for older recovery playbooks. In that mode this
     script writes a deterministic task spec JSON to
     ``<work_dir>/feature-pr-specs/<slug>-feature-pr<N>.json`` and uses the
     historical process-grep/file-lock idempotency guard.

Exit codes:
  0 — spec written + monitor spawned (or no-op because a monitor was
      already running).
  1 — lookup failed, PR not open, bad repo URL, etc.

Usage::

    ./scripts/attach_feature_pr_monitor.py \\
        --repo git@github.com:dimileeh/aira-web.git \\
        --pr 277 \\
        --agent claude_code \\
        --companions scripts/airaweb_only.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from awf.common.commands import (  # noqa: E402
    AsyncCommandRunner,
    AsyncioSubprocessRunner,
)
from awf.common.github_client import RepoRef  # noqa: E402
from awf.runtime.feature_pr_sync import (  # noqa: E402
    FeaturePRSyncError,
    build_sync_feature_pr_task_spec,
    fetch_pr_metadata,
    is_feature_pr_monitor_running,
    task_spec_filename,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argparse factory — exposed so tests can verify flag parsing
    without the ``sys.exit`` side effect that ``parser.parse_args`` of
    ``sys.argv`` would normally have."""
    parser = argparse.ArgumentParser(
        description=(
            "Attach an AWF PR monitor to an already-open feature PR. "
            "For failed-workspace recovery or external PRs."
        )
    )
    parser.add_argument(
        "--repo",
        help='Repo URL or slug, e.g. "git@github.com:dimileeh/aira-web.git".',
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="PR number to attach the monitor to.",
    )
    parser.add_argument(
        "--pr-url",
        default=None,
        help="Full GitHub PR URL. When supplied, --repo/--pr are optional.",
    )
    parser.add_argument(
        "--agent",
        default="codex",
        help="Coding CLI to use when the monitor needs to address "
        "comments or fix CI failures. Default: codex.",
    )
    parser.add_argument(
        "--no-auto-merge",
        dest="auto_merge",
        action="store_false",
        help="Opt OUT of auto-merging. Default is to auto-merge when "
        "all gates turn green — that's AWF's contract for feature→dev "
        "PRs. Pass this flag for one-off recovery runs where you want "
        "to review the final state before landing.",
    )
    parser.set_defaults(auto_merge=True)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/tmp/awf-realrun"),
        help="AWF work directory (shared with other AWF invocations).",
    )
    parser.add_argument(
        "--companions",
        type=Path,
        default=None,
        help="Optional JSON file with a list of companion service specs. "
        "Usually required — the monitor may invoke the coding CLI, "
        "which needs the same companion stack the PR's validation "
        "originally depended on.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AWF_CLI_BASE_URL", "http://localhost:8000"),
        help="AWF API base URL for the supported adoption endpoint.",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("AWF_API_TOKEN"),
        help="AWF API bearer token. Defaults to AWF_API_TOKEN.",
    )
    parser.add_argument(
        "--legacy-detached",
        action="store_true",
        help="Use the deprecated detached run_awf.py monitor path.",
    )
    ns = parser.parse_args(argv)
    if ns.legacy_detached and (not ns.repo or ns.pr is None):
        parser.error("--legacy-detached requires --repo plus --pr")
    if not ns.pr_url and (not ns.repo or ns.pr is None):
        parser.error("provide --pr-url, or provide --repo plus --pr")
    return ns


@contextlib.contextmanager
def _per_pr_lock(work_dir: Path, pr_number: int) -> Iterator[bool]:
    """Serialize the spec-write + spawn on a per-PR lock file so
    concurrent watchdog invocations can't double-spawn.

    Yields ``True`` if the caller acquired the lock and should do the
    full attach flow; yields ``False`` if another process already holds
    it (treat as idempotent no-op, exit 0).

    The lock file persists on disk; only the ``flock`` is released on
    context exit. Leaving the file in place is fine — the next attach
    invocation will reuse it. No cleanup race because flock is
    fd-scoped, not path-scoped.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / f"feature-pr-monitor-{pr_number}.lock"
    fd = lock_path.open("w")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another attach invocation owns the lock — let it handle
            # the spawn, we exit cleanly.
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


async def orchestrate_attach(
    *,
    repo_url: str,
    pr_number: int,
    agent: str,
    auto_merge: bool,
    companions_path: Path | None,
    work_dir: Path,
    runner: AsyncCommandRunner,
    spawn: Callable[..., Any] = subprocess.Popen,
    process_lister: Callable[[], str] | None = None,
) -> int:
    """Main async driver — composes the steps and returns an exit code.

    Extracted from ``main`` so tests can inject a ``FakeCommandRunner``
    and a fake ``spawn`` / ``process_lister``. The real ``main`` passes
    ``AsyncioSubprocessRunner`` and ``subprocess.Popen`` / ``None``.
    """
    # Parse URL upfront so we fail fast on malformed input (no gh call).
    try:
        repo = RepoRef.from_url(repo_url)
    except ValueError as exc:
        print(f"attach-feature-pr-monitor ERROR: {exc}", file=sys.stderr)
        return 1

    # Resolve PR metadata. Refuses closed/merged PRs inside
    # fetch_pr_metadata so we don't race to provision a workspace for
    # something that can't transition.
    try:
        metadata = await fetch_pr_metadata(runner=runner, repo=repo, pr_number=pr_number)
    except FeaturePRSyncError as exc:
        print(f"attach-feature-pr-monitor ERROR: {exc}", file=sys.stderr)
        return 1

    companions = _load_companions(companions_path) if companions_path else None

    spec = build_sync_feature_pr_task_spec(
        repo_url=repo_url,
        metadata=metadata,
        agent=agent,
        auto_merge=auto_merge,
        companions=companions,
    )
    spec_filename = task_spec_filename(repo_slug=repo.slug(), pr_number=pr_number)

    # Serialize: the ps-grep idempotency check races with a concurrent
    # invocation that's already PAST the check but hasn't called Popen
    # yet. A per-PR fcntl.flock closes that window. Watchdog polls can
    # hammer attach without double-spawning.
    with _per_pr_lock(work_dir, pr_number) as acquired:
        if not acquired:
            print(
                f"attach-feature-pr-monitor: another invocation is already "
                f"attaching {repo.slug()}#{pr_number}; exiting idempotent.",
                flush=True,
            )
            return 0

        # Re-check ps INSIDE the lock — the losing invocation from a
        # previous race may have already spawned the monitor between
        # our ps check and our lock acquisition.
        if is_feature_pr_monitor_running(
            spec_filename=spec_filename, process_lister=process_lister
        ):
            print(
                f"attach-feature-pr-monitor: monitor already active for "
                f"{repo.slug()}#{pr_number}; nothing to do.",
                flush=True,
            )
            return 0

        spec_dir = work_dir / "feature-pr-specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / spec_filename
        # run_awf.py expects a LIST of task specs — wrap ours in one.
        spec_path.write_text(json.dumps([spec], indent=2))

        # Spawn run_awf.py detached. --keep-state is retained for
        # compatibility with older runners; current run_awf.py preserves
        # PostgreSQL-backed run state by default.
        #
        # ``sys.executable`` (not a hardcoded ``.venv/bin/python``) so the
        # script works under ``uv run``, system python, or a venv rooted
        # anywhere other than ``<repo>/.venv`` — sibling scheduler scripts
        # already follow this pattern.
        log_path = work_dir / f"feature-pr-monitor-{pr_number}.log"
        run_awf = _ROOT / "scripts" / "run_awf.py"
        with log_path.open("a") as log_file:
            handle = spawn(
                [
                    sys.executable,
                    str(run_awf),
                    "--config",
                    str(spec_path),
                    "--work-dir",
                    str(work_dir),
                    "--keep-state",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(
            f"attach-feature-pr-monitor: spawned run_awf pid={getattr(handle, 'pid', '?')} "
            f"for {repo.slug()}#{pr_number}; log: {log_path}",
            flush=True,
        )
        return 0


async def orchestrate_service_adoption(
    *,
    repo_url: str | None,
    pr_number: int | None,
    pr_url: str | None,
    agent: str,
    auto_merge: bool,
    companions_path: Path | None,
    work_dir: Path,
    base_url: str | None,
    api_token: str | None,
) -> int:
    """Call AWF's supported existing-PR adoption API.

    The detached ``run_awf.py`` path remains available behind
    ``--legacy-detached`` for old recovery playbooks, but the default now
    creates an AWF service-managed workspace and monitor lineage.
    """
    # ``work_dir`` only controls the deprecated detached runner. The
    # service-managed adoption endpoint uses the AWF service's configured
    # workspace root, so this wrapper intentionally does not forward it.
    del work_dir
    print(
        "attach-feature-pr-monitor: using supported AWF PR adoption API; "
        "pass --legacy-detached to use the deprecated run_awf.py path.",
        file=sys.stderr,
    )
    if companions_path is not None:
        print(
            "attach-feature-pr-monitor: --companions is ignored by the "
            "service-managed adoption path; profile settings now own runtime services.",
            file=sys.stderr,
        )

    repo_value = repo_url.strip() if isinstance(repo_url, str) else None
    repo_slug = None
    normalized_repo_url = None
    if repo_value:
        if "github.com" in repo_value:
            normalized_repo_url = repo_value
        else:
            repo_slug = repo_value

    payload = {
        "repo_url": normalized_repo_url,
        "repo_slug": repo_slug,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "agent": agent,
        "profile_ref": "auto",
        "profile": None,
        "auto_merge": auto_merge,
        "initial_review_grace_period_seconds": None,
        "task_title": None,
        "task_prompt": None,
        "reason": "legacy attach-feature-pr-monitor wrapper",
    }
    headers = {}
    token = api_token or os.environ.get("AWF_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{(base_url or os.environ.get('AWF_CLI_BASE_URL') or 'http://localhost:8000').rstrip('/')}/v1/workspaces/adopt-pr"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        print(f"attach-feature-pr-monitor ERROR: could not reach AWF API: {exc}", file=sys.stderr)
        return 1

    if response.status_code >= 400:
        print(
            f"attach-feature-pr-monitor ERROR: AWF adoption failed "
            f"({response.status_code}): {response.text}",
            file=sys.stderr,
        )
        return 1
    try:
        print(json.dumps(response.json(), indent=2), flush=True)
    except ValueError:
        print(response.text, flush=True)
    return 0


def _load_companions(path: Path) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads(path.read_text()))


async def _main(ns: argparse.Namespace) -> int:
    if not getattr(ns, "legacy_detached", False):
        return await orchestrate_service_adoption(
            repo_url=ns.repo,
            pr_number=ns.pr,
            pr_url=getattr(ns, "pr_url", None),
            agent=ns.agent,
            auto_merge=ns.auto_merge,
            companions_path=ns.companions,
            work_dir=ns.work_dir,
            base_url=getattr(ns, "base_url", None),
            api_token=getattr(ns, "api_token", None),
        )

    runner = AsyncioSubprocessRunner()
    return await orchestrate_attach(
        repo_url=ns.repo,
        pr_number=ns.pr,
        agent=ns.agent,
        auto_merge=ns.auto_merge,
        companions_path=ns.companions,
        work_dir=ns.work_dir,
        runner=runner,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(parse_args())))
