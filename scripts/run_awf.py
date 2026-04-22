"""One-shot driver: run an AWF workspace end-to-end against a real repo.

Usage:
    python scripts/run_awf.py --config scripts/run_awf_tasks.json

The JSON config is a list of task objects; each is submitted concurrently
and the script waits for all of them to reach a terminal state before
exiting. Example object::

    {
      "repo_url": "git@github.com:dimileeh/aira-agent.git",
      "branch_base": "development",
      "task_title": "Add module docstring",
      "task_prompt": "...",
      "agent": "claude_code",
      "test_commands": ["ruff check .", "pytest tests/unit -q"],
      "requires_database": false
    }

Prints a terminal-state summary per task at the end.

This script is the glue between the AWF building blocks and the host
environment; it is NOT a replacement for the upcoming worker-based
orchestration — just a pragmatic way to exercise the full pipeline from
the operator's laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Adapter registry side-effect import (populates get_adapter).
import awf.adapters.registry  # noqa: F401
from awf.common.commands import AsyncioSubprocessRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import (
    AuthMount,
    CompanionService,
    ComposeManager,
    WorkspaceComposeSpec,
)
from awf.adapters.base import AgentAdapter
from awf.common.github_client import GitHubClient
from awf.node.git_manager import GitManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.release_pr_monitor import build_feature_pr_monitor
from awf.runtime.validation import ValidationRunner

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "docker" / "compose" / "workspace.base.yml.j2"

# Empty defaults → let each CLI read its own ~/.<cli>/config for model choice.
# This avoids shipping a model name that's wrong for one account type (e.g. gpt-5.1
# is not available on ChatGPT-account Codex, which uses gpt-5.4).
_DEFAULT_MODELS: dict[AgentRuntime, str] = {}


@dataclass(frozen=True)
class CompanionConfig:
    """JSON-serializable companion spec. Mirrors ``CompanionService`` plus
    an optional ``repo_url`` — if set, the driver clones the companion's
    repo to a dedicated host dir and uses that as the build context.
    If unset, ``build_context`` must already point at an existing path."""

    name: str
    build_context: str | None = None  # required when repo_url is None
    repo_url: str | None = None  # optional: clone this, use the checkout as build context
    branch: str = "development"
    dockerfile: str = "Dockerfile"
    env_file: str | None = None
    environment: dict[str, str] | None = None
    depends_on: list[str] | None = None
    healthcheck_cmd: str | None = None
    ports: list[list[int]] | None = None
    command: str | None = None


@dataclass(frozen=True)
class TaskConfig:
    repo_url: str
    branch_base: str
    task_title: str
    task_prompt: str
    agent: str
    test_commands: list[str]
    requires_database: bool = False
    companions: list[dict[str, Any]] | None = None


def _build_auth_mounts(host_home: Path) -> list[AuthMount]:
    """Map host CLI credential directories to the agent user's home.

    The container user is ``agent`` with home ``/home/agent`` (UID 1000,
    matching the host user so bind-mounted files are readable).

    Mount mode notes:
    - ``.codex``, ``.claude``, ``.gemini`` are ``rw`` because each CLI writes
      its model cache / token-refresh state inside its home directory; a
      read-only mount breaks session initialization.
    - ``.config/gh``, ``.gitconfig``, ``.ssh`` are ``ro`` — stable credentials
      with no state to update during a task run.
    """
    container_home = "/home/agent"
    rw_mounts = [
        (host_home / ".codex", f"{container_home}/.codex", "rw"),
        (host_home / ".claude", f"{container_home}/.claude", "rw"),
        # Claude Code keeps its top-level config as a single file at
        # ``~/.claude.json`` (separate from the ``~/.claude/`` dir). When
        # only the dir is mounted, Claude either boots on a hollow default
        # config or — more often — invalidates the file mid-session on
        # token refresh and dies with "Claude configuration file not
        # found at: /home/agent/.claude.json". Mount the file itself too.
        (host_home / ".claude.json", f"{container_home}/.claude.json", "rw"),
        (host_home / ".gemini", f"{container_home}/.gemini", "rw"),
    ]
    ro_mounts = [
        (host_home / ".config" / "gh", f"{container_home}/.config/gh", "ro"),
        (host_home / ".gitconfig", f"{container_home}/.gitconfig", "ro"),
        (host_home / ".ssh", f"{container_home}/.ssh", "ro"),
    ]
    return [
        AuthMount(source=str(src), target=tgt, mode=mode)
        for src, tgt, mode in [*rw_mounts, *ro_mounts]
        if src.exists()
    ]


async def _materialize_companion(
    raw: dict[str, Any],
    *,
    git: GitManager,
) -> CompanionService:
    """Resolve one JSON companion block to a CompanionService.

    If ``repo_url`` is set, we clone it to a dedicated companion worktree
    under ``<work_dir>/companions/<name>`` and use that as the build context.
    Otherwise ``build_context`` is taken verbatim (must already exist).
    """
    name = raw["name"]
    if raw.get("repo_url"):
        # Companion gets its own mirror + worktree, checked out at the base
        # branch (no feature branch — we don't edit companions). The
        # companion_ws_id is deterministic, so a failed prior run can leave
        # the worktree on disk; remove it first so add_worktree sees a clean
        # slate. Companions are read-only build contexts — we don't commit
        # back to them, so nothing is lost by re-creating them.
        companion_ws_id = f"companion__{name}"
        await git.remove_worktree(
            workspace_id=companion_ws_id,
            repo_url=raw["repo_url"],
        )
        layout = await git.add_worktree(
            workspace_id=companion_ws_id,
            repo_url=raw["repo_url"],
            base_branch=raw.get("branch", "development"),
            new_branch=f"awf-companion/{name}-{os.getpid()}",
        )
        build_context = str(layout.worktree_path)
    else:
        build_context = raw["build_context"]

    return CompanionService(
        name=name,
        build_context=build_context,
        dockerfile=raw.get("dockerfile", "Dockerfile"),
        env_file=raw.get("env_file"),
        environment=tuple((k, v) for k, v in (raw.get("environment") or {}).items()),
        depends_on=tuple(raw.get("depends_on") or ("postgres",)),
        healthcheck_cmd=raw.get("healthcheck_cmd"),
        ports=tuple((cp, hp) for cp, hp in (raw.get("ports") or [])),
        command=raw.get("command"),
    )


async def _run_task(
    cfg: TaskConfig,
    *,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    auth_mounts: list[AuthMount],
    git_name: str,
    git_email: str,
) -> dict[str, Any]:
    runner = AsyncioSubprocessRunner()
    git = GitManager(work_dir / "git")
    compose = ComposeManager(work_dir=work_dir / "compose", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=runner, artifacts_dir=work_dir / "artifacts")
    pr_creator = PullRequestCreator(runner)

    # Step 1: create the workspace in DB.
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=cfg.repo_url,
            branch_base=cfg.branch_base,
            task_title=cfg.task_title,
            task_prompt=cfg.task_prompt,
            agent=cfg.agent,
            test_commands=cfg.test_commands,
            requires_database=cfg.requires_database,
        )
        await s.commit()
        ws_id = ws.id

    print(f"[{cfg.task_title[:40]}] workspace = {ws_id}", flush=True)

    # Step 2: claim + provisioning (git worktree).
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        await repo.transition(persisted, to=WorkspaceStatus.provisioning, reason_code="DRIVER")
        await s.commit()

    branch_name = f"awf/{ws_id}"
    layout = await git.add_worktree(
        workspace_id=ws_id,
        repo_url=cfg.repo_url,
        base_branch=cfg.branch_base,
        new_branch=branch_name,
    )
    base_commit = await git.head_sha(workspace_id=ws_id)

    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        persisted.branch_name = branch_name
        persisted.base_commit = base_commit
        persisted.compose_project_name = f"awf_{ws_id}"
        await s.commit()

    # Step 3: clone + resolve any companion repos. Companions may reference a
    # ``${POSTGRES_URL}`` placeholder in their env overrides — we expand it to
    # the stack-local DB URL here so the driver owns password generation and
    # the companions read the right connection string.
    postgres_password = "awf_dev_" + ws_id[-8:]  # deterministic per workspace
    postgres_url = f"postgresql+asyncpg://awf:{postgres_password}@postgres:5432/awf"
    companion_services: list[CompanionService] = []
    for comp_raw in cfg.companions or []:
        resolved = dict(comp_raw)
        if resolved.get("environment"):
            resolved["environment"] = {
                k: (v.replace("${POSTGRES_URL}", postgres_url) if isinstance(v, str) else v)
                for k, v in resolved["environment"].items()
            }
        companion_services.append(await _materialize_companion(resolved, git=git))

    # Step 4: compose up with auth mounts + companions.
    # ── git-in-container ──────────────────────────────────────────────────
    # The worktree's ``.git`` file contains an absolute host path pointing at
    # the mirror's ``worktrees/<ws_id>`` admin dir. Without mounting the mirror
    # at the same absolute path inside the container, in-container ``git``
    # fails with "fatal: not a git repository: <host-path>". Coding CLIs
    # (Claude Code especially) sanity-check git state before making edits
    # and refuse to proceed if it's broken.
    mirror_mount = AuthMount(
        source=str(layout.mirror_path),
        target=str(layout.mirror_path),
        mode="rw",
    )
    spec = WorkspaceComposeSpec(
        workspace_id=ws_id,
        worktree_host_path=layout.worktree_path,
        # aira-backend requires the ``vector`` Postgres extension for embeddings;
        # plain postgres:16-alpine doesn't include it. Use the pgvector image
        # everywhere — harmless extra KB for tasks that don't need the extension.
        postgres_image="pgvector/pgvector:pg18",
        postgres_password=postgres_password,
        auth_mounts=(mirror_mount, *auth_mounts),
        git_name=git_name,
        git_email=git_email,
        companions=tuple(companion_services),
    )
    print(f"[{cfg.task_title[:40]}] compose up ...", flush=True)
    await compose.up(spec, wait=True)
    print(f"[{cfg.task_title[:40]}] compose up OK", flush=True)

    # Step 4: transition to ready.
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        await repo.transition(persisted, to=WorkspaceStatus.ready, reason_code="STACK_READY")
        await s.commit()

    # Step 5: execute. Wire the PR monitor factory — the executor calls
    # it once it has the per-task adapter, and the returned monitor drives
    # the ``monitoring_pr`` stage (comments, CI, base sync, merge).
    gh = GitHubClient(runner)

    def _monitor_factory(adapter: AgentAdapter):
        return build_feature_pr_monitor(
            session_factory=session_factory,
            runner=runner,
            adapter=adapter,
            gh=gh,
            worktrees_root=work_dir / "git" / "worktrees",
        )

    executor = WorkspaceExecutor(
        session_factory=session_factory,
        runner=runner,
        compose=compose,
        validation=validation,
        pr_creator=pr_creator,
        config=ExecutorConfig(
            worktrees_root=work_dir / "git" / "worktrees",
            compose_projects_root=work_dir / "compose" / "compose",
            default_models=_DEFAULT_MODELS,
        ),
        pr_monitor_factory=_monitor_factory,
    )
    print(f"[{cfg.task_title[:40]}] executor starting ...", flush=True)
    await executor.execute(ws_id)

    # Step 6: final state.
    async with session_factory() as s:
        persisted = await WorkspaceRepository(s).get(ws_id)
        assert persisted is not None
        return {
            "workspace_id": ws_id,
            "title": cfg.task_title,
            "status": persisted.status,
            "pr_url": persisted.pr_url,
            "failure_reason": persisted.failure_reason,
            "failure_message": persisted.failure_message,
            "branch": persisted.branch_name,
            "base_commit": persisted.base_commit,
        }


async def _main(config_path: Path, work_dir: Path, keep_state: bool) -> int:
    with config_path.open() as f:
        raw = json.load(f)
    tasks = [TaskConfig(**t) for t in raw]
    print(f"Running {len(tasks)} task(s) from {config_path}", flush=True)

    host_home = Path(os.environ["HOME"])
    auth_mounts = _build_auth_mounts(host_home)
    print(f"Auth mounts: {[m.target for m in auth_mounts]}", flush=True)

    # Git identity for commits — read from host gitconfig.
    import subprocess

    git_name = subprocess.run(
        ["git", "config", "--global", "user.name"], capture_output=True, text=True
    ).stdout.strip()
    git_email = subprocess.run(
        ["git", "config", "--global", "user.email"], capture_output=True, text=True
    ).stdout.strip()

    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "awf.db"
    if db_path.exists() and not keep_state:
        db_path.unlink()
    engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)

    try:
        results = await asyncio.gather(
            *(
                _run_task(
                    t,
                    work_dir=work_dir,
                    session_factory=factory,
                    auth_mounts=auth_mounts,
                    git_name=git_name,
                    git_email=git_email,
                )
                for t in tasks
            ),
            return_exceptions=True,
        )
    finally:
        await engine.dispose()

    print("\n" + "=" * 60, flush=True)
    print("AWF RUN RESULTS", flush=True)
    print("=" * 60, flush=True)
    all_ok = True
    for r in results:
        if isinstance(r, BaseException):
            print(f"✗ EXCEPTION: {r!r}", flush=True)
            all_ok = False
            continue
        result: dict[str, Any] = r
        mark = "✓" if result["status"] == "completed" else "✗"
        print(
            f"{mark} {result['title']}\n   status: {result['status']}\n   pr_url: {result.get('pr_url')}",
            flush=True,
        )
        if result.get("failure_reason"):
            print(
                f"   reason: {result['failure_reason']}\n   msg: {result.get('failure_message')}",
                flush=True,
            )
            all_ok = False
        elif result["status"] != "completed":
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path, default=Path.home() / ".awf" / "runs" / "default")
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Don't delete the SQLite DB from a previous run in the same work dir.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.config, args.work_dir, args.keep_state)))
