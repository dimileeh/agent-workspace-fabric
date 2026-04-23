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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Adapter registry side-effect import (populates get_adapter).
import awf.adapters.registry  # noqa: F401
from awf.adapters.base import AgentAdapter
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.github_client import GitHubClient, RepoRef
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
    task_kind: str = "feature_branch_pr"
    """One of the values in ``awf.db.enums.TaskKind``. ``feature_branch_pr``
    is the default and most common; ``sync_release_pr`` skips the agent
    run + validation + feature-branch PR create and attaches the release
    monitor (auto_merge=False) directly to an existing open PR."""

    source_branch: str | None = None
    """For ``sync_release_pr`` only. The source branch whose commits need
    to land on ``branch_base``. Defaults to ``development`` if unset."""

    pr_number: int | None = None
    """For ``sync_release_pr`` only. The number of the already-open
    ``<source_branch> → <branch_base>`` release PR to monitor. If
    unset, the driver queries GitHub for it and errors out if none
    exists (the scheduler should always populate this)."""

    auto_merge: bool = False
    """For ``sync_feature_pr`` only. ``True`` → monitor lands the PR
    itself once all gates turn green; ``False`` → monitor posts a
    "ready to merge" notification and waits for a human. Safe default
    is ``False`` because the CLI's most common caller (failed-workspace
    recovery) wants deliberate human review rather than silent merges."""


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
    owner_workspace_id: str,
) -> CompanionService:
    """Resolve one JSON companion block to a CompanionService.

    If ``repo_url`` is set, we clone it to a dedicated companion worktree
    under ``<work_dir>/companions/<workspace_id>__<name>`` and use that
    as the build context. Otherwise ``build_context`` is taken verbatim
    (must already exist).

    ``owner_workspace_id`` scopes the companion's worktree ID so two
    concurrent workspaces each materializing a companion with the same
    NAME (e.g. both need a ``backend`` companion from aira-agent) don't
    stomp on a shared ``companion__backend`` path. The previous naming
    was ``companion__{name}`` — dangerous under ``asyncio.gather()``,
    where two ``_run_task()`` coroutines race to ``remove_worktree`` +
    ``add_worktree`` at the same filesystem path. Review feedback on
    PR #2 (CodeRabbit, Critical): "Scope companion worktree IDs to
    the owning workspace".
    """
    name = raw["name"]
    if raw.get("repo_url"):
        # Companion gets its own mirror + worktree, checked out at the base
        # branch (no feature branch — we don't edit companions). The
        # companion_ws_id is deterministic per (owner_workspace, name)
        # so a failed prior run's worktree can be removed cleanly; a
        # concurrent workspace with a same-named companion lands in a
        # different path. Companions are read-only build contexts — we
        # don't commit back to them, so nothing is lost by re-creating
        # them.
        companion_ws_id = f"companion__{owner_workspace_id}__{name}"
        await git.remove_worktree(
            workspace_id=companion_ws_id,
            repo_url=raw["repo_url"],
        )
        layout = await git.add_worktree(
            workspace_id=companion_ws_id,
            repo_url=raw["repo_url"],
            base_branch=raw.get("branch", "development"),
            new_branch=f"awf-companion/{owner_workspace_id}-{name}",
        )
        build_context = str(layout.worktree_path)
    else:
        build_context = raw["build_context"]

    env_file_raw = raw.get("env_file")
    env_file = _expand_host_path(env_file_raw) if env_file_raw else None
    return CompanionService(
        name=name,
        build_context=build_context,
        dockerfile=raw.get("dockerfile", "Dockerfile"),
        env_file=env_file,
        environment=tuple((k, v) for k, v in (raw.get("environment") or {}).items()),
        depends_on=tuple(raw.get("depends_on") or ("postgres",)),
        healthcheck_cmd=raw.get("healthcheck_cmd"),
        ports=tuple((cp, hp) for cp, hp in (raw.get("ports") or [])),
        command=raw.get("command"),
    )


async def _configure_branch_push_upstream(
    *,
    runner: AsyncioSubprocessRunner,
    worktree_path: Path,
    branch_name: str,
    remote_branch: str,
) -> None:
    """Configure a per-workspace branch so ``git push`` writes back to
    the *remote* branch it's tracking.

    Used by the ``sync_release_pr`` and ``sync_feature_pr`` handlers,
    which check out a local ref like ``release-sync/ws_<id>`` or
    ``feature-sync/ws_<id>`` that should push to a specific remote
    branch (``development``, the PR's head, etc.). Extracted from the
    handlers so the three identical blocks can't drift out of sync.
    Review feedback on PR #2 (CodeRabbit, generic CLI concern at
    run_awf.py:487).

    Sets three git configs on the worktree:
      * ``branch.<branch>.remote = origin``
      * ``branch.<branch>.merge = refs/heads/<remote_branch>``
      * ``push.default = upstream`` — so bare ``git push`` writes
        HEAD to whatever the two above specify.
    """
    for cfg_args in (
        [f"branch.{branch_name}.remote", "origin"],
        [f"branch.{branch_name}.merge", f"refs/heads/{remote_branch}"],
        ["push.default", "upstream"],
    ):
        await runner.run(["git", "-C", str(worktree_path), "config", *cfg_args])


_DEFAULT_FALLBACK_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


def _expand_host_path(path: str) -> str:
    """Expand ``~``, ``$VAR`` / ``${VAR}``, and ``${VAR:-default}`` in a
    host-side path string.

    Companion specs reference host files by path (typically ``.env``
    files on the operator's machine). The progression of portability
    options:

      * Hardcoded absolute — ``/home/dimileeh/Projects/aira/…`` —
        locks the spec to one developer's filesystem.
      * ``~/...`` — portable across Unix users but assumes the same
        directory structure under each HOME.
      * ``${VAR}/...`` — needs the operator to set VAR; silently
        fails if unset (``expandvars`` leaves ``${VAR}`` literal and
        we'd try to open a nonexistent file).
      * ``${VAR:-default}/...`` — best of both worlds: operator can
        override with VAR, spec works out of the box if VAR is unset.
        Shell syntax, so spec authors already know it.

    Review feedback on PRs #2, #4 (CodeRabbit + gemini, repeated):
    the plain ``~/Projects/aira`` path wasn't fully portable. This
    helper now understands the shell ``${VAR:-default}`` idiom, so
    a spec can say ``${AWF_AIRA_CHECKOUT_ROOT:-~/Projects/aira}/…``
    and an operator on a machine without that env var + without the
    exact directory gets a clear error ("no such file") instead of
    a silent broken mount.
    """

    # Pre-expand ``${VAR:-default}`` BEFORE ``os.path.expandvars`` runs,
    # because expandvars doesn't understand the :- syntax and would
    # leave it literal.
    def _resolve_fallback(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        return os.environ.get(var_name, default)

    with_fallbacks = _DEFAULT_FALLBACK_PATTERN.sub(_resolve_fallback, path)
    # Then the standard ${VAR} / $VAR expansion (for specs that
    # deliberately don't want a fallback).
    expanded = os.path.expandvars(with_fallbacks)
    return str(Path(expanded).expanduser())


async def _run_task_with_failure_guard(
    cfg: TaskConfig,
    *,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    auth_mounts: list[AuthMount],
    git_name: str,
    git_email: str,
) -> dict[str, Any]:
    """Wrap ``_run_task`` so an unhandled exception in the handler
    still transitions the workspace row to ``failed``.

    Without this guard, a crash in compose-up / git-add-worktree / any
    other provisioning step leaves the DB row stuck in a non-terminal
    state forever (observed in production: runaway watcher spawned 122
    workspaces, each crashed on ``all predefined address pools have
    been fully subnetted``, none got marked failed, DB-based
    idempotency checks downstream thought each was still running).

    The guard identifies the stuck workspace by querying the DB for
    the MOST RECENT row matching this task's ``repo_url`` + ``task_title``
    that's in a non-terminal state. It's a heuristic — the ``_run_*``
    handlers don't currently expose the workspace id they created to
    the caller — but it's robust enough: each handler creates exactly
    one workspace at task start, and by the time an exception
    propagates up, that row is the latest non-terminal one for the
    pair.
    """
    try:
        return await _run_task(
            cfg,
            work_dir=work_dir,
            session_factory=session_factory,
            auth_mounts=auth_mounts,
            git_name=git_name,
            git_email=git_email,
        )
    except BaseException as exc:
        # Any exception — compose failure, git error, KeyboardInterrupt.
        # Mark the orphaned workspace row failed so downstream
        # idempotency checks (the scheduler's DB-based
        # ``_monitor_already_running``) don't see a phantom "active"
        # workspace that will never transition on its own.
        await _mark_orphan_workspace_failed(
            session_factory=session_factory,
            repo_url=cfg.repo_url,
            task_title=cfg.task_title,
            message=f"driver crashed mid-provision: {exc!r}"[:2000],
        )
        raise


async def _mark_orphan_workspace_failed(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repo_url: str,
    task_title: str,
    message: str,
) -> None:
    """Mark the most recent non-terminal workspace for this
    (repo_url, task_title) as failed. No-op if none found."""
    from sqlalchemy import select

    from awf.db.models import Workspace

    async with session_factory() as s:
        result = await s.execute(
            select(Workspace)
            .where(
                Workspace.repo_url == repo_url,
                Workspace.task_title == task_title,
                Workspace.status.notin_(("completed", "failed")),
            )
            .order_by(Workspace.created_at.desc())
            .limit(1)
        )
        ws = result.scalar_one_or_none()
        if ws is None:
            return
        ws.status = WorkspaceStatus.failed.value
        ws.failure_reason = "infrastructure_failure"
        ws.failure_message = message
        await s.commit()


async def _run_task(
    cfg: TaskConfig,
    *,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    auth_mounts: list[AuthMount],
    git_name: str,
    git_email: str,
) -> dict[str, Any]:
    # Dispatch by task kind. ``sync_release_pr`` skips the coding agent
    # run + validation + feature-branch PR creation; it just provisions
    # a worktree on the source branch, spins up the compose stack, and
    # attaches the release-PR monitor (auto_merge=False) to an
    # already-open development→main PR.
    if cfg.task_kind == "sync_release_pr":
        return await _run_sync_release_pr(
            cfg,
            work_dir=work_dir,
            session_factory=session_factory,
            auth_mounts=auth_mounts,
            git_name=git_name,
            git_email=git_email,
        )
    if cfg.task_kind == "sync_feature_pr":
        return await _run_sync_feature_pr(
            cfg,
            work_dir=work_dir,
            session_factory=session_factory,
            auth_mounts=auth_mounts,
            git_name=git_name,
            git_email=git_email,
        )
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
        companion_services.append(
            await _materialize_companion(resolved, git=git, owner_workspace_id=ws_id)
        )

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


async def _run_sync_release_pr(
    cfg: TaskConfig,
    *,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    auth_mounts: list[AuthMount],
    git_name: str,
    git_email: str,
) -> dict[str, Any]:
    """Attach the release-PR monitor to an already-open ``source → target`` PR.

    No coding agent runs here. No feature branch is created. The
    workspace's worktree IS the source branch (typically
    ``development``) — when the monitor invokes the coding CLI to
    address review comments on the release PR, its fix commits land
    on ``development``, and the push updates origin/development which
    the PR naturally reflects.

    ``cfg.pr_number`` MUST be set by the caller (scheduler /
    ``attach_release_monitor.py`` / watcher). If unset, this function
    errors out — the scheduler's job is to call ``ensure_release_pr_open``
    first so a PR exists.
    """
    from awf.runtime.release_pr_monitor import build_release_pr_monitor

    if cfg.pr_number is None:
        raise ValueError(
            "sync_release_pr requires cfg.pr_number — the scheduler must "
            "confirm the PR exists before launching this task"
        )
    source_branch = cfg.source_branch or "development"
    target_branch = cfg.branch_base

    runner = AsyncioSubprocessRunner()
    git = GitManager(work_dir / "git")
    compose = ComposeManager(work_dir=work_dir / "compose", template_path=_TEMPLATE)

    # Step 1: workspace row. For a release-sync, branch_name is the
    # source branch (not an awf/ws_X feature branch) because the
    # monitor's fix commits go directly onto ``development``.
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=cfg.repo_url,
            branch_base=target_branch,
            task_title=cfg.task_title,
            task_prompt=cfg.task_prompt,
            agent=cfg.agent,
            test_commands=[],
            requires_database=cfg.requires_database,
        )
        ws.task_kind = "sync_release_pr"
        await s.commit()
        ws_id = ws.id

    print(f"[{cfg.task_title[:40]}] workspace = {ws_id} (sync_release_pr)", flush=True)

    # Step 2: provisioning — worktree on the source branch.
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        await repo.transition(
            persisted, to=WorkspaceStatus.provisioning, reason_code="DRIVER_SYNC_RELEASE"
        )
        await s.commit()

    # We create a fresh local ref ``release-sync/ws_<id>`` that tracks
    # ``origin/<source_branch>``. Using a per-workspace ref (rather than
    # checking out ``development`` directly) avoids the git limitation
    # that the same branch can't be checked out in two worktrees — so
    # multiple concurrent sync workspaces on the same repo don't race
    # on the shared ``development`` ref. The monitor's ``git push
    # origin HEAD`` resolves to pushing that per-workspace ref BACK to
    # ``origin/<source_branch>`` via a refspec we set below.
    branch_name = f"release-sync/{ws_id}"
    layout = await git.add_worktree(
        workspace_id=ws_id,
        repo_url=cfg.repo_url,
        base_branch=source_branch,
        new_branch=branch_name,
    )
    await _configure_branch_push_upstream(
        runner=runner,
        worktree_path=layout.worktree_path,
        branch_name=branch_name,
        remote_branch=source_branch,
    )
    base_commit = await git.head_sha(workspace_id=ws_id)

    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        persisted.branch_name = branch_name
        persisted.base_commit = base_commit
        persisted.compose_project_name = f"awf_{ws_id}"
        persisted.pr_url = (
            f"https://github.com/{RepoRef.from_url(cfg.repo_url).slug()}/pull/{cfg.pr_number}"
        )
        persisted.pr_number = cfg.pr_number
        await s.commit()

    # Step 3: materialize companions (same as feature-branch flow).
    postgres_password = "awf_dev_" + ws_id[-8:]
    postgres_url = f"postgresql+asyncpg://awf:{postgres_password}@postgres:5432/awf"
    companion_services: list[CompanionService] = []
    for comp_raw in cfg.companions or []:
        resolved = dict(comp_raw)
        if resolved.get("environment"):
            resolved["environment"] = {
                k: (v.replace("${POSTGRES_URL}", postgres_url) if isinstance(v, str) else v)
                for k, v in resolved["environment"].items()
            }
        companion_services.append(
            await _materialize_companion(resolved, git=git, owner_workspace_id=ws_id)
        )

    # Step 4: compose up (agent container + postgres + companions).
    mirror_mount = AuthMount(
        source=str(layout.mirror_path),
        target=str(layout.mirror_path),
        mode="rw",
    )
    spec = WorkspaceComposeSpec(
        workspace_id=ws_id,
        worktree_host_path=layout.worktree_path,
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

    # Step 5: walk the state machine directly to monitoring_pr —
    # release-sync skips the agent-run/validate/push stages.
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        for to_state, reason in [
            (WorkspaceStatus.ready, "SYNC_STACK_READY"),
            (WorkspaceStatus.running, "SYNC_SKIP_AGENT"),
            (WorkspaceStatus.validating, "SYNC_SKIP_VALIDATE"),
            (WorkspaceStatus.pushing, "SYNC_SKIP_PUSH"),
            (WorkspaceStatus.monitoring_pr, "SYNC_RELEASE_ATTACH_MONITOR"),
        ]:
            await repo.transition(persisted, to=to_state, reason_code=reason)
        await s.commit()

    # Step 6: attach the release-PR monitor (auto_merge=False).
    from awf.adapters.base import get_adapter

    agent_runtime = AgentRuntime(cfg.agent)
    adapter = get_adapter(agent_runtime, runner=runner, default_model=None)
    gh = GitHubClient(runner)
    monitor = build_release_pr_monitor(
        session_factory=session_factory,
        runner=runner,
        adapter=adapter,
        gh=gh,
        worktrees_root=work_dir / "git" / "worktrees",
    )
    print(
        f"[{cfg.task_title[:40]}] release-monitor running for PR #{cfg.pr_number} ...",
        flush=True,
    )
    compose_file = work_dir / "compose" / "compose" / ws_id / "compose.yml"
    await monitor.run(
        workspace_id=ws_id,
        compose_project=f"awf_{ws_id}",
        compose_file=compose_file,
    )

    # Step 7: final state.
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


async def _run_sync_feature_pr(
    cfg: TaskConfig,
    *,
    work_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    auth_mounts: list[AuthMount],
    git_name: str,
    git_email: str,
) -> dict[str, Any]:
    """Attach an AWF PR monitor to an already-open feature PR.

    Shares 95% of its shape with ``_run_sync_release_pr`` — the
    differences are:

      - **Source branch.** Here it's the PR's actual head branch
        (``cfg.source_branch``, populated by
        ``scripts/attach_feature_pr_monitor.py``). No fallback to
        ``development`` — a feature PR without a head branch name is
        a programming error upstream.
      - **Worktree ref prefix.** ``feature-sync/ws_<id>`` (vs
        ``release-sync/ws_<id>``) so grep-based operator inspection
        can tell the two kinds apart at a glance.
      - **Monitor factory.** Selected by ``cfg.auto_merge``: the two
        existing factories differ only in the ``auto_merge`` toggle,
        so we pick the matching one rather than plumbing a third.
    """
    from awf.runtime.release_pr_monitor import (
        build_feature_pr_monitor,
        build_release_pr_monitor,
    )

    if cfg.pr_number is None:
        raise ValueError(
            "sync_feature_pr requires cfg.pr_number — the CLI "
            "(attach_feature_pr_monitor.py) must populate it before invoking run_awf"
        )
    if not cfg.source_branch:
        raise ValueError(
            "sync_feature_pr requires cfg.source_branch — the PR's head "
            "branch must be resolved upfront (fetch_pr_metadata)"
        )
    source_branch = cfg.source_branch
    target_branch = cfg.branch_base

    runner = AsyncioSubprocessRunner()
    git = GitManager(work_dir / "git")
    compose = ComposeManager(work_dir=work_dir / "compose", template_path=_TEMPLATE)

    # Step 1: workspace row.
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=cfg.repo_url,
            branch_base=target_branch,
            task_title=cfg.task_title,
            task_prompt=cfg.task_prompt,
            agent=cfg.agent,
            test_commands=[],
            requires_database=cfg.requires_database,
        )
        ws.task_kind = "sync_feature_pr"
        await s.commit()
        ws_id = ws.id

    print(
        f"[{cfg.task_title[:40]}] workspace = {ws_id} (sync_feature_pr, "
        f"auto_merge={cfg.auto_merge})",
        flush=True,
    )

    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        await repo.transition(
            persisted,
            to=WorkspaceStatus.provisioning,
            reason_code="DRIVER_SYNC_FEATURE",
        )
        await s.commit()

    # Step 2: worktree on the PR's head branch. Same per-workspace-ref
    # trick as release-sync (avoids "branch checked out in two
    # worktrees" git error when concurrent attaches run on the same
    # head branch) — the ref ``feature-sync/<ws_id>`` tracks
    # ``origin/<head_branch>`` and push.default=upstream makes
    # ``git push`` write back to the PR's branch.
    branch_name = f"feature-sync/{ws_id}"
    layout = await git.add_worktree(
        workspace_id=ws_id,
        repo_url=cfg.repo_url,
        base_branch=source_branch,
        new_branch=branch_name,
    )
    await _configure_branch_push_upstream(
        runner=runner,
        worktree_path=layout.worktree_path,
        branch_name=branch_name,
        remote_branch=source_branch,
    )
    base_commit = await git.head_sha(workspace_id=ws_id)

    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        persisted.branch_name = branch_name
        persisted.base_commit = base_commit
        persisted.compose_project_name = f"awf_{ws_id}"
        persisted.pr_url = (
            f"https://github.com/{RepoRef.from_url(cfg.repo_url).slug()}/pull/{cfg.pr_number}"
        )
        persisted.pr_number = cfg.pr_number
        await s.commit()

    # Step 3: materialize companions (same as feature-branch / release-sync).
    postgres_password = "awf_dev_" + ws_id[-8:]
    postgres_url = f"postgresql+asyncpg://awf:{postgres_password}@postgres:5432/awf"
    companion_services: list[CompanionService] = []
    for comp_raw in cfg.companions or []:
        resolved = dict(comp_raw)
        if resolved.get("environment"):
            resolved["environment"] = {
                k: (v.replace("${POSTGRES_URL}", postgres_url) if isinstance(v, str) else v)
                for k, v in resolved["environment"].items()
            }
        companion_services.append(
            await _materialize_companion(resolved, git=git, owner_workspace_id=ws_id)
        )

    # Step 4: compose up.
    mirror_mount = AuthMount(
        source=str(layout.mirror_path),
        target=str(layout.mirror_path),
        mode="rw",
    )
    spec = WorkspaceComposeSpec(
        workspace_id=ws_id,
        worktree_host_path=layout.worktree_path,
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

    # Step 5: walk the state machine directly to monitoring_pr — the
    # code is already on the PR; no agent run, no validation, no push.
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        persisted = await repo.get(ws_id)
        assert persisted is not None
        for to_state, reason in [
            (WorkspaceStatus.ready, "SYNC_STACK_READY"),
            (WorkspaceStatus.running, "SYNC_SKIP_AGENT"),
            (WorkspaceStatus.validating, "SYNC_SKIP_VALIDATE"),
            (WorkspaceStatus.pushing, "SYNC_SKIP_PUSH"),
            (WorkspaceStatus.monitoring_pr, "SYNC_FEATURE_ATTACH_MONITOR"),
        ]:
            await repo.transition(persisted, to=to_state, reason_code=reason)
        await s.commit()

    # Step 6: attach the PR monitor, toggling auto_merge per the spec.
    from awf.adapters.base import get_adapter

    agent_runtime = AgentRuntime(cfg.agent)
    adapter = get_adapter(agent_runtime, runner=runner, default_model=None)
    gh = GitHubClient(runner)
    factory = build_feature_pr_monitor if cfg.auto_merge else build_release_pr_monitor
    monitor = factory(
        session_factory=session_factory,
        runner=runner,
        adapter=adapter,
        gh=gh,
        worktrees_root=work_dir / "git" / "worktrees",
    )
    print(
        f"[{cfg.task_title[:40]}] feature-pr-monitor running for PR "
        f"#{cfg.pr_number} (auto_merge={cfg.auto_merge}) ...",
        flush=True,
    )
    compose_file = work_dir / "compose" / "compose" / ws_id / "compose.yml"
    await monitor.run(
        workspace_id=ws_id,
        compose_project=f"awf_{ws_id}",
        compose_file=compose_file,
    )

    # Step 7: final state.
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
                _run_task_with_failure_guard(
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
