"""Project-free diagnostic bootstrap into an AWF managed linked worktree."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from awf.common.agent_execution import execution_from_policy
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import WorktreeLayout
from awf.node.git_manager_registration import worktree_add_lock_args
from awf.node.stack_launcher import WorkspaceStackLaunchRequest
from awf.profiles.models import WorkspaceProfile
from awf.runtime.provider_diagnostic_fixture import seed_fixture
from awf.service.provider_diagnostics import INTERNAL_REPO


async def _git(*args: str, input_data: bytes | None = None) -> str:
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "AWF",
        "GIT_AUTHOR_EMAIL": "awf@localhost",
        "GIT_COMMITTER_NAME": "AWF",
        "GIT_COMMITTER_EMAIL": "awf@localhost",
    }
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await process.communicate(input_data)
    except BaseException:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError("PROVIDER_DIAGNOSTIC_FIXTURE_FAILED")
    return stdout.decode().strip()


async def fixture_worktree(manager: Any, workspace_id: str) -> WorktreeLayout:
    mirror = manager._mirror_path(INTERNAL_REPO)
    worktree = manager._worktree_path_for(workspace_id)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = "awf-diagnostic/" + workspace_id
    async with manager._lock_for_mirror(mirror):
        # Initialization may have stopped after mkdir or before the fixture ref.
        # Repair in place so registered worktrees and their branches survive.
        if not (mirror / "HEAD").exists():
            await _git("init", "--bare", str(mirror))
        refs = await _git(
            "--git-dir", str(mirror), "for-each-ref", "--format=%(refname)", "refs/heads/fixture"
        )
        if "refs/heads/fixture" not in refs.splitlines():
            tree = await _git("--git-dir", str(mirror), "mktree", input_data=b"")
            commit = await _git(
                "--git-dir",
                str(mirror),
                "commit-tree",
                tree,
                input_data=b"AWF internal diagnostic fixture\n",
            )
            await _git("--git-dir", str(mirror), "update-ref", "refs/heads/fixture", commit)
        await _git(
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            *worktree_add_lock_args(workspace_id),
            "-b",
            branch,
            str(worktree),
            "fixture",
        )
        seed_fixture(worktree)
    await manager._prepare_agent_writable_worktree(layout_mirror=mirror, worktree_path=worktree)
    return WorktreeLayout(mirror_path=mirror, worktree_path=worktree, branch_name=branch)


async def provision_diagnostic(provisioner: Any, workspace: Any, *, epoch: int | None) -> None:
    workspace_id = workspace.id
    try:
        from awf.service.provider_diagnostics import require_diagnostic_contract

        require_diagnostic_contract(workspace)
        if provisioner._stack_launcher is None:
            raise ValueError("PROVIDER_DIAGNOSTIC_RUNTIME_UNSUPPORTED")
        layout = await fixture_worktree(provisioner._git, workspace_id)
        profile = WorkspaceProfile.model_validate(workspace.resolved_profile)
        if await dispose_if_terminal(provisioner, workspace_id, launched=False):
            return
        if epoch is not None and not await provisioner._verify_execution_claim_epoch(
            workspace_id, epoch
        ):
            return
        # Publish paths before launch, so cancellation/worker-loss can tear down.
        can_launch = False
        async with provisioner._session_factory() as session:
            ws = await WorkspaceRepository(session).get_for_update(workspace_id)
            if (
                ws is not None
                and ws.status == "provisioning"
                and (epoch is None or ws.execution_claim_epoch == epoch)
            ):
                ws.branch_name = layout.branch_name
                ws.compose_project_name = "awf_" + workspace_id
                ws.node_id = provisioner._config.node_id
                await session.commit()
                can_launch = True
        if not can_launch:
            await dispose_if_terminal(provisioner, workspace_id, launched=False)
            return
        paths = await provisioner._stack_launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id=workspace_id,
                layout=layout,
                profile=profile,
                execution=execution_from_policy(workspace.task_policy),
                diagnostic=True,
            )
        )
        if await dispose_if_terminal(provisioner, workspace_id, launched=True):
            return
        published = False
        async with provisioner._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(workspace_id)
            if (
                ws is not None
                and ws.status == "provisioning"
                and (epoch is None or ws.execution_claim_epoch == epoch)
            ):
                if paths is not None:
                    ws.compose_file_path = str(paths.compose_file)
                await repo.transition(
                    ws, to=WorkspaceStatus.ready, reason_code="PROVIDER_DIAGNOSTIC_READY"
                )
                await session.commit()
                published = True
        if not published:
            await dispose_if_terminal(provisioner, workspace_id, launched=True)
    except asyncio.CancelledError:
        await dispose_if_terminal(provisioner, workspace_id, launched=True)
        raise
    except (ValueError, OSError, ComposeOperationError) as error:
        from awf.db.enums import FailureReason

        await provisioner._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.provisioning,
            failure_reason=FailureReason.infrastructure_failure,
            reason_code="PROVIDER_DIAGNOSTIC_BOOTSTRAP_FAILED",
            message=type(error).__name__,
            execution_claim_epoch=epoch,
        )
        await settle_bootstrap_failure(provisioner, workspace_id, epoch=epoch)


async def dispose_if_terminal(provisioner: Any, workspace_id: str, *, launched: bool) -> bool:
    """Reap a late fixture/stack after cancellation without touching a new claim."""
    from awf.db.repositories.base import (
        TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
        TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    )
    from awf.node.cleanup import WorkspaceCleaner
    from awf.node.git_manager import GitOperationError
    from awf.service.provider_diagnostics import settle_quiescent_diagnostic

    async with provisioner._session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        if ws is None or ws.status not in {"cancelled", "destroyed", "destroying", "failed"}:
            return False
    ok = False
    cleanup_reason = "PROVIDER_DIAGNOSTIC_RUNTIME_UNSUPPORTED"
    try:
        compose = getattr(provisioner._stack_launcher, "_compose", None)
        auth_resolver = getattr(provisioner._stack_launcher, "_auth_mount_resolver", None)
        auth_work_dir = getattr(auth_resolver, "work_dir", None)
        if compose is not None:
            result = await WorkspaceCleaner(
                git=provisioner._git, compose=compose, auth_work_dir=auth_work_dir
            ).cleanup(
                workspace_id=workspace_id,
                repo_url=INTERNAL_REPO,
                compose_project_name=ws.compose_project_name,
                compose_file_path=Path(ws.compose_file_path) if ws.compose_file_path else None,
                remove_volumes=True,
                remove_worktree=True,
            )
            ok = result.ok
            cleanup_reason = result.reason_code
        elif not launched and not ws.task_policy.get("provider_diagnostic", {}).get(
            "invocation_started_at"
        ):
            await provisioner._git.remove_worktree(
                workspace_id=workspace_id, repo_url=INTERNAL_REPO
            )
            if auth_work_dir is not None:
                remove_diagnostic_auth(auth_work_dir, workspace_id)
            ok = True
            cleanup_reason = "CLEANUP_SUCCEEDED"
    except (OSError, ValueError, GitOperationError) as error:
        cleanup_reason = getattr(error, "reason_code", "PROVIDER_DIAGNOSTIC_CLEANUP_FAILED")
    async with provisioner._session_factory() as session:
        repo = WorkspaceRepository(session)
        current = await repo.get_for_update(workspace_id)
        if current is not None and current.status in {
            "cancelled",
            "destroyed",
            "destroying",
            "failed",
        }:
            await repo.add_event(
                current,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE
                if ok
                else TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE
                if ok
                else TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                payload={
                    "source": "provider_diagnostic_late_provision",
                    "cleanup_succeeded": ok,
                    "cleanup_reason": cleanup_reason,
                },
            )
            if ok and current.status != "destroying":
                await settle_quiescent_diagnostic(session, current)
            await session.commit()
    return True


def diagnostic_auth_mounts(mounts: list[Any], agent: str | None) -> list[Any]:
    kept = []
    for mount in mounts:
        if mount.source == mount.target or mount.target in {
            "/home/agent/.codex/auth.json",
            "/home/agent/.claude/.credentials.json",
        }:
            kept.append(mount)
        elif mount.target == "/home/agent/.codex" and agent == "codex":
            if any(path.name != "auth.json" for path in Path(mount.source).iterdir()):
                raise ValueError("PROVIDER_DIAGNOSTIC_PROFILE_UNSUPPORTED")
            kept.append(mount)
        elif mount.target == "/home/agent/.claude" and agent == "claude_code":
            if any(path.name != ".credentials.json" for path in Path(mount.source).iterdir()):
                raise ValueError("PROVIDER_DIAGNOSTIC_PROFILE_UNSUPPORTED")
            kept.append(mount)
        elif (
            not mount.target.startswith("/home/agent/")
            and mount.target != "/opt/awf/selected-provider-source.json"
        ):
            kept.append(mount)
    return kept


def remove_diagnostic_auth(work_dir: Path, workspace_id: str) -> None:
    import re
    import shutil

    if not re.fullmatch(r"ws_[A-Za-z0-9_]+", workspace_id):
        raise ValueError("PROVIDER_DIAGNOSTIC_FIXTURE_FAILED")
    root = work_dir / "auth" / workspace_id
    directory = root / "diagnostic"
    if root.is_symlink() or directory.is_symlink():
        raise ValueError("PROVIDER_DIAGNOSTIC_FIXTURE_FAILED")
    if directory.exists():
        shutil.rmtree(directory)
    if root.exists() and not any(root.iterdir()):
        root.rmdir()


async def settle_bootstrap_failure(
    provisioner: Any, workspace_id: str, *, epoch: int | None
) -> None:
    from awf.db.enums import OperationStatus, OperationType
    from awf.db.repositories import OperationRepository
    from awf.service.provider_diagnostics import DIAGNOSTIC_KEY, DIAGNOSTIC_KIND

    async with provisioner._session_factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        if (
            ws is None
            or ws.status != "failed"
            or (epoch is not None and ws.execution_claim_epoch != epoch)
        ):
            return
        result = {
            "outcome": "inconclusive",
            "reason_code": "PROVIDER_DIAGNOSTIC_BOOTSTRAP_FAILED",
            "fixture_verification": False,
            "protocol_evidence": "inconclusive",
        }
        ws.task_policy = {
            **ws.task_policy,
            DIAGNOSTIC_KEY: {
                **ws.task_policy[DIAGNOSTIC_KEY],
                "state": "inconclusive",
                "result": result,
            },
        }
        for operation in await OperationRepository(session).list_for_workspace(
            workspace_id, operation_type=OperationType.start
        ):
            if (operation.payload or {}).get("source") == DIAGNOSTIC_KIND and operation.status in {
                "pending",
                "running",
            }:
                await OperationRepository(session).finish(
                    operation, status=OperationStatus.failed, result=result
                )
        await session.commit()
