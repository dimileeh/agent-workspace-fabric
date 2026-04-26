"""Stranded-PR watchdog.

If a ``run_awf.py`` monitor process dies (OOM, Docker restart, SIGKILL)
the PR it was watching is stranded — no process pulls in base-branch
updates, addresses new review comments, or retries the merge. This
watchdog is the external answer: every ``poll_seconds`` it lists the
open ``awf/``-prefixed PRs in each watched repo and re-attaches the
monitor (via ``scripts/attach_feature_pr_monitor.py``) for any PR with
no matching process.

The watchdog is intentionally a separate long-running CLI — NOT
auto-started from ``create_app`` — so the operator's mental model
stays simple: "one watchdog per host, runs forever, fires attach
scripts". The attach script is itself idempotent (see the file-lock in
``scripts/attach_feature_pr_monitor.py``) so repeated invocations from
concurrent watchdog instances can never double-spawn.

Entry point: ``awf-watchdog start --work-dir <dir> --poll-seconds
300``. See ``main()`` for the full invocation.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import typer

from awf.common.logging import get_logger
from awf.runtime.feature_pr_sync import task_spec_filename

_log = get_logger(__name__)


_DEFAULT_REPOS = [
    "dimileeh/aira-agent",
    "dimileeh/aira-web",
    "dimileeh/aira-agent-workspace-fabric",
]


@dataclass(frozen=True)
class WatchdogConfig:
    """Operator-facing knobs. Defaults match the three AWF-managed repos.

    ``repos`` lists ``owner/name`` slugs — the watchdog calls
    ``gh pr list`` against each one with ``--head awf/``.

    ``work_dir`` is shared with all AWF invocations on the host; the
    attach script writes per-PR spec files under
    ``<work_dir>/feature-pr-specs/``.

    ``agent`` is the coding CLI name the attach script passes through
    to ``run_awf.py`` (``claude_code`` / ``codex`` / etc.).

    ``poll_seconds`` controls how often the loop wakes up. 300s is a
    reasonable default — long enough that we don't burn gh API quota,
    short enough that a dead monitor gets re-attached within minutes.

    ``gh_pr_limit`` caps the ``gh pr list`` page size per repo. 200 is
    comfortably above any realistic open-PR count for the three
    AWF-managed repos, but raise it if a repo ever holds more open
    ``awf/`` PRs than this — otherwise stranded PRs past the cap are
    silently skipped."""

    work_dir: Path
    repos: list[str] = field(default_factory=lambda: list(_DEFAULT_REPOS))
    agent: str = "claude_code"
    poll_seconds: int = 300
    gh_pr_limit: int = 200


# ── Scan primitives — pure functions taking injectable seams ───────────────


def _default_gh_lister(repo: str, *, limit: int = 200) -> str:
    """Real implementation: shell out to ``gh pr list``. Returns the
    raw JSON string from gh's stdout. Raises ``subprocess.CalledProcessError``
    on non-zero exit (callers catch + continue).

    ``limit`` maps directly to ``gh pr list --limit``. The watchdog
    binds this via ``functools.partial`` from ``WatchdogConfig.gh_pr_limit``
    so operators can raise it if a repo ever exceeds the default.

    ``headRefOid`` is requested so the NotifyHuman-aware respawn-skip
    can compare the PR's current head SHA against the SHA recorded in
    the latest defer-signal artifact — without it, a NotifyHuman PR
    that received new commits would never re-engage."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--head",
            "awf/",
            "--json",
            "number,headRefName,headRefOid,baseRefName",
            "--limit",
            str(limit),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _default_process_lister() -> str:
    """Real implementation: shell out to ``ps -ef`` so a watchdog can
    detect monitor processes regardless of how they were launched
    (``Popen`` detach, ``systemd`` unit, ``nohup``, etc.)."""
    try:
        return subprocess.check_output(
            ["ps", "-ef"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def _project_root() -> Path:
    """Walk upward from this file until we hit the ``pyproject.toml``
    that anchors the AWF checkout. Used to locate ``scripts/`` without
    hard-coding ``parents[N]``, which silently breaks if this module
    ever gets re-nested under ``src/``."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(
        f"awf-watchdog: could not locate pyproject.toml above {here}; "
        "the watchdog must run from within the AWF source checkout"
    )


def _default_spawn_attach(*, repo: str, pr_number: int, work_dir: Path, agent: str) -> int:
    """Real implementation: invoke the attach CLI as a subprocess so the
    watchdog never shares a Python interpreter / asyncio loop / env
    with the monitor it spawns."""
    attach_script = _project_root() / "scripts" / "attach_feature_pr_monitor.py"
    result = subprocess.run(
        [
            sys.executable,
            str(attach_script),
            "--repo",
            f"git@github.com:{repo}.git",
            "--pr",
            str(pr_number),
            "--work-dir",
            str(work_dir),
            "--agent",
            agent,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _log.warning(
            "watchdog.attach_script_failed",
            repo=repo,
            pr=pr_number,
            returncode=result.returncode,
            stderr=(result.stderr or "")[:400],
        )
    return result.returncode


def _extract_owner_name(repo: str) -> tuple[str, str]:
    """Split ``owner/name`` — used to compose the deterministic spec
    filename we pgrep for."""
    owner, _, name = repo.partition("/")
    return owner, name


def _latest_defer_signal_for_pr(
    artifacts_root: Path, *, owner: str, repo: str, pr_number: int
) -> dict | None:
    """Scan ``artifacts_root`` for the most-recent ``*.defer-signal.json``
    whose payload matches this owner/repo/pr_number triple. Returns the
    parsed JSON or None if no match is found. Corrupt files are skipped
    silently — the watchdog's default behaviour (respawn) covers the
    fall-through case correctly.

    Multiple workspaces can drive the same PR over its lifetime
    (a workspace exits, the operator re-attaches, etc.); we use file
    mtime to pick the freshest artifact so a stale NotifyHuman cannot
    silence a more recent Abort."""
    if not artifacts_root.is_dir():
        return None
    expected_slug = f"{owner}/{repo}"
    candidates: list[tuple[float, dict]] = []
    for path in artifacts_root.glob("*.defer-signal.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("pr_number") != pr_number:
            continue
        if payload.get("repo") != expected_slug:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, payload))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _should_skip_respawn(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    current_head_sha: str,
    artifacts_root: Path,
) -> bool:
    """Return True iff the previous monitor for this PR exited via
    ``NotifyHuman`` AND the PR's head_sha hasn't moved since.

    Crash recovery is the watchdog's primary purpose, so the default for
    every uncertain case is False (respawn). We only skip when there is
    POSITIVE evidence the previous run reached a terminal that explicitly
    handed control back to a human and there's nothing new to look at."""
    signal = _latest_defer_signal_for_pr(
        artifacts_root, owner=owner, repo=repo, pr_number=pr_number
    )
    if signal is None:
        return False  # crash recovery — never silence
    if signal.get("terminal_action") != "NotifyHuman":
        return False  # Merge / Abort / ShortCircuitCompleted → respawn
    artifact_sha = str(signal.get("head_sha") or "")
    if not artifact_sha or artifact_sha != current_head_sha:
        return False  # new commits arrived → re-engage
    _log.info(
        "watchdog.skipped_notify_human_terminal",
        repo=f"{owner}/{repo}",
        pr_number=pr_number,
        head_sha=current_head_sha[:10],
        reason="awaiting human merge or new commits",
    )
    return True


def _pr_already_monitored(*, repo: str, pr_number: int, ps_output: str) -> bool:
    """True iff ``ps_output`` contains a process line that references
    the deterministic spec filename for this repo+PR.

    The attach script writes specs at
    ``<work_dir>/feature-pr-specs/<owner>__<name>-feature-pr<N>.json``
    and ``run_awf.py`` receives the path via ``--config``. Grepping the
    filename alone is sufficient — both owner/name and PR number are
    embedded."""
    owner, name = _extract_owner_name(repo)
    slug = f"{owner}/{name}"
    spec_name = task_spec_filename(repo_slug=slug, pr_number=pr_number)
    return any(spec_name in line for line in ps_output.splitlines())


def run_one_scan(
    *,
    config: WatchdogConfig,
    gh_lister: Callable[[str], str],
    process_lister: Callable[[], str],
    spawn_attach: Callable[..., int],
) -> None:
    """Execute one full scan across all configured repos.

    Exposed as a plain function (not a method) so tests can drive it
    synchronously with fakes; the long-running ``main`` just calls this
    in a loop.

    Guarantees:
      * Per-repo ``gh`` failures are caught and logged — the scan
        moves on to the next repo and never raises.
      * JSON parse errors on a repo's gh output are treated the same
        as a lookup failure (the output must have been corrupted).
      * An attach-script failure for one PR does not stop processing
        of the remaining PRs on the same repo.
    """
    _log.info("watchdog.scan_start", repos=config.repos)

    ps_output = process_lister()

    for repo in config.repos:
        try:
            raw = gh_lister(repo)
        except Exception as exc:
            _log.warning("watchdog.gh_lookup_failed", repo=repo, error=repr(exc)[:400])
            continue

        try:
            prs = json.loads(raw)
        except json.JSONDecodeError as exc:
            _log.warning(
                "watchdog.gh_output_unparseable",
                repo=repo,
                error=str(exc),
                raw_excerpt=(raw or "")[:200],
            )
            continue

        if not isinstance(prs, list):
            _log.warning("watchdog.gh_output_wrong_shape", repo=repo, raw=repr(prs)[:200])
            continue

        owner, name = _extract_owner_name(repo)
        for pr in prs:
            pr_number = pr.get("number") if isinstance(pr, dict) else None
            if not isinstance(pr_number, int):
                _log.warning("watchdog.pr_missing_number", repo=repo, pr=repr(pr)[:200])
                continue

            if _pr_already_monitored(repo=repo, pr_number=pr_number, ps_output=ps_output):
                _log.info("watchdog.pr_already_monitored", repo=repo, pr=pr_number)
                continue

            head_sha = pr.get("headRefOid") if isinstance(pr, dict) else None
            if _should_skip_respawn(
                owner=owner,
                repo=name,
                pr_number=pr_number,
                current_head_sha=str(head_sha or ""),
                artifacts_root=config.work_dir / "artifacts",
            ):
                continue

            _log.info("watchdog.respawning_monitor", repo=repo, pr=pr_number)
            try:
                spawn_attach(
                    repo=repo,
                    pr_number=pr_number,
                    work_dir=config.work_dir,
                    agent=config.agent,
                )
            except Exception as exc:
                _log.warning(
                    "watchdog.attach_spawn_raised",
                    repo=repo,
                    pr=pr_number,
                    error=repr(exc)[:400],
                )

    _log.info("watchdog.scan_complete")


# ── Long-running loop ─────────────────────────────────────────────────────


class _ShutdownFlag:
    """Tiny SIGTERM/SIGINT observer. Kept as a class so ``main`` can
    install handlers without mutating module globals."""

    def __init__(self) -> None:
        self.stop = False

    def handle(self, *_: object) -> None:
        self.stop = True


def _run_loop(
    *,
    config: WatchdogConfig,
    gh_lister: Callable[[str], str],
    process_lister: Callable[[], str],
    spawn_attach: Callable[..., int],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Scan forever on ``config.poll_seconds`` cadence. Returns only on
    SIGTERM/SIGINT."""
    flag = _ShutdownFlag()
    signal.signal(signal.SIGTERM, flag.handle)
    signal.signal(signal.SIGINT, flag.handle)

    while not flag.stop:
        run_one_scan(
            config=config,
            gh_lister=gh_lister,
            process_lister=process_lister,
            spawn_attach=spawn_attach,
        )
        # Sleep in 1s slices so a SIGTERM exits the loop promptly.
        remaining = config.poll_seconds
        while remaining > 0 and not flag.stop:
            sleep(1.0)
            remaining -= 1


# ── Typer CLI ─────────────────────────────────────────────────────────────


app = typer.Typer(
    name="awf-watchdog",
    help="Watch for stranded AWF PR monitors and re-attach them.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.command("start")
def start(
    work_dir: Path = typer.Option(
        Path("/tmp/awf-realrun"),  # noqa: S108 — matches attach_feature_pr_monitor default
        "--work-dir",
        help="AWF work directory (shared with the attach script + run_awf.py).",
    ),
    poll_seconds: int = typer.Option(
        300,
        "--poll-seconds",
        help="Seconds between scans. 300 is a reasonable default.",
    ),
    repo: list[str] = typer.Option(
        None,
        "--repo",
        help="Repo slug (owner/name). Repeatable. Defaults to the three AWF-managed repos.",
    ),
    agent: str = typer.Option(
        "claude_code",
        "--agent",
        help="Coding CLI the attach script passes to run_awf.py.",
    ),
    gh_pr_limit: int = typer.Option(
        200,
        "--gh-pr-limit",
        help="Max PRs fetched per repo via 'gh pr list --limit'. Raise if any repo has >200 open awf/ PRs.",
    ),
) -> None:
    """Run the watchdog loop (blocks forever until SIGTERM/SIGINT)."""
    config = WatchdogConfig(
        work_dir=work_dir,
        repos=list(repo) if repo else list(_DEFAULT_REPOS),
        agent=agent,
        poll_seconds=poll_seconds,
        gh_pr_limit=gh_pr_limit,
    )
    _run_loop(
        config=config,
        gh_lister=partial(_default_gh_lister, limit=config.gh_pr_limit),
        process_lister=_default_process_lister,
        spawn_attach=_default_spawn_attach,
    )


def main() -> None:  # pragma: no cover — entry point
    app(prog_name="awf-watchdog")


if __name__ == "__main__":  # pragma: no cover
    main()
