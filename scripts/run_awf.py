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
from awf.node.compose_manager import AuthMount, ComposeManager, WorkspaceComposeSpec
from awf.node.git_manager import GitManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "docker" / "compose" / "workspace.base.yml.j2"

# Empty defaults → let each CLI read its own ~/.<cli>/config for model choice.
# This avoids shipping a model name that's wrong for one account type (e.g. gpt-5.1
# is not available on ChatGPT-account Codex, which uses gpt-5.4).
_DEFAULT_MODELS: dict[AgentRuntime, str] = {}


@dataclass(frozen=True)
class TaskConfig:
    repo_url: str
    branch_base: str
    task_title: str
    task_prompt: str
    agent: str
    test_commands: list[str]
    requires_database: bool = False


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

    # Step 3: compose up with auth mounts.
    spec = WorkspaceComposeSpec(
        workspace_id=ws_id,
        worktree_host_path=layout.worktree_path,
        auth_mounts=tuple(auth_mounts),
        git_name=git_name,
        git_email=git_email,
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

    # Step 5: execute.
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
