"""Canonical paused recovery with real HTTP/PG/Git and synthetic command effects."""

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.api.routes.controls import _controls
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.config import Settings, get_settings
from awf.control.worker import manager
from awf.db.models import Workspace
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.service import provider_readiness, worker
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    origin_repo as origin_repo,
)
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_retained import deny_agent
from tests.unit.test_hosted_runtime_stop_outcome import settings_for


@pytest.mark.parametrize(
    "reason,phase,cause",
    [
        (reason, phase, "authorized_cancel")
        for reason in ("blocked", "recovering")
        for phase in (
            "before_scope",
            "setup",
            "agent",
            "after_pause" if reason == "blocked" else "budget_decision",
            "advanced",
            "initial_pause",
        )
    ]
    + [
        (reason, phase, cause)
        for reason in ("blocked", "recovering")
        for phase in ("setup", "agent", "after_pause" if reason == "blocked" else "budget_decision")
        for cause in ("revoke", "expiry")
    ]
    + [
        (reason, phase, "cleanup_failure")
        for reason in ("blocked", "recovering")
        for phase in ("setup", "agent")
    ]
    + [(reason, "agent", "revoke_overlap") for reason in ("blocked", "recovering")],
)
async def test_authorized_paused_resume_retains_warm_stack(
    authority, origin_repo, tmp_path, monkeypatch, reason, phase, cause
):
    runtime, payload, factory = authority
    settings = await settings_for(factory, tmp_path)
    work = Path(settings.work_dir)
    scope = WorkspaceConsumers(runtime, work)
    scope.poll_seconds = 0.02
    payload["repo"]["base_branch"] = "development"
    payload["task"]["auto_merge"] = False
    payload["workspace"] = {
        "profile_ref": None,
        "profile": {"name": "paused-cancel", "phases": {"setup": ["printf synthetic-setup"]}},
    }
    (origin_repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n")
    subprocess.run(
        ["git", "add", "pyproject.toml"], cwd=origin_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "fixture protected configuration"],
        cwd=origin_repo,
        check=True,
        capture_output=True,
    )
    stage = "initial"
    warm = False
    commands = []
    entered = asyncio.Event()
    retry_entered = asyncio.Event()
    hold = asyncio.Event()
    final_check_entered = asyncio.Event()
    overlap_armed = False
    real = AsyncioSubprocessRunner()
    spawn = asyncio.create_subprocess_exec

    def docker_result(args):
        nonlocal warm
        commands.append({"stage": stage, "args": list(args), "warm_before": warm})
        if cause == "cleanup_failure" and stage == "resuming" and "awf-cleanup" in args:
            return CommandResult(1, "", "awf cleanup: tagged processes still alive")
        if "down" in args:
            warm = False
        if "up" in args:
            warm = True
        if "exec" in args and not warm:
            return CommandResult(1, "", 'service "agent" is not running')
        return CommandResult(0, "[]" if "ps" in args or "inspect" in args else "", "")

    async def local_spawn(*args, **kwargs):
        if args[0] == "docker":
            result = docker_result(args)
            return await spawn(
                sys.executable, "-c", f"raise SystemExit({result.returncode})", **kwargs
            )
        args = tuple(str(origin_repo) if item == payload["repo"]["url"] else item for item in args)
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_spawn)
    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )

    class Effects:
        async def run(self, args, **kwargs):
            if args[0] == "git":
                if stage == "resuming" and phase == "advanced" and "push" in args:
                    entered.set()
                    await hold.wait()
                return await real.run(args, **kwargs)
            assert args[0] == "docker", args
            return docker_result(args)

        async def run_streaming(self, args, **kwargs):
            if args[0] == "git":
                return await real.run_streaming(args, **kwargs)
            assert args[:2] == ["docker", "compose"], args
            result = docker_result(args)
            if result.returncode:
                return result
            if (
                stage == "resuming"
                and phase == "setup"
                and any("synthetic-setup" in arg for arg in args)
            ):
                entered.set()
                await hold.wait()
            if "codex" in args and args[args.index("codex") + 1 :][:1] == ["exec"]:
                assert scope.workspace_id.get() == "ws_cloud_stable"
                if stage == "initial" or (
                    stage == "resuming" and phase in {"after_pause", "budget_decision"}
                ):
                    if reason == "recovering":
                        return CommandResult(
                            1,
                            "",
                            "OpenAI HTTP 429 Too Many Requests",
                            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                        )
                    (work / "git/worktrees/ws_cloud_stable/pyproject.toml").write_text(
                        "[tool.pytest.ini_options]\naddopts = '-qq'\n"
                    )
                    (work / "git/worktrees/ws_cloud_stable/change.txt").write_text(
                        "synthetic change\n"
                    )
                elif stage == "resuming" and phase == "advanced":
                    (work / "git/worktrees/ws_cloud_stable/pyproject.toml").write_text(
                        "[tool.pytest.ini_options]\naddopts = '-q'\n"
                    )
                    (work / "git/worktrees/ws_cloud_stable/change.txt").write_text(
                        "synthetic resumed change\n"
                    )
                elif stage == "resuming":
                    entered.set()
                    await hold.wait()
                elif stage == "retry":
                    retry_entered.set()
                    await hold.wait()
            return result

    monkeypatch.setattr(worker, "AsyncioSubprocessRunner", Effects)
    built = worker.build_worker_runtime(settings, hosted=scope)
    original_resume = built.worker._executor.resume_paused_execution

    async def resume_boundary(*args, **kwargs):
        if stage == "resuming" and phase == "before_scope":
            entered.set()
            await hold.wait()
        return await original_resume(*args, **kwargs)

    monkeypatch.setattr(built.worker._executor, "resume_paused_execution", resume_boundary)
    pause_name = (
        "enter_blocked_for_protected_violation"
        if reason == "blocked"
        else "enter_recovering_for_provider_failure"
    )
    original_pause = getattr(built.worker._executor, pause_name)

    async def after_pause_boundary(*args, **kwargs):
        result = await original_pause(*args, **kwargs)
        if (stage == "resuming" and phase in {"after_pause", "budget_decision"}) or (
            stage == "initial" and phase == "initial_pause"
        ):
            observations["pause_result"] = str(result)
            state = await row_state()
            if phase != "budget_decision":
                assert state["status"] == reason, state
            else:
                assert state["status"] == "running" and result.value == "terminal", (state, result)
            entered.set()
            await hold.wait()
        return result

    monkeypatch.setattr(built.worker._executor, pause_name, after_pause_boundary)
    original_workspace = runtime.workspace

    async def final_authority_boundary(*args, **kwargs):
        task = asyncio.current_task()
        if (
            overlap_armed
            and scope.workspace_id.get() is None
            and not task.get_name().startswith("hosted-authority-")
        ):
            final_check_entered.set()
            await hold.wait()
        return await original_workspace(*args, **kwargs)

    if cause == "revoke_overlap":
        monkeypatch.setattr(runtime, "workspace", final_authority_boundary)

    class Clock(datetime):
        selected_now = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.selected_now if tz is not None else cls.selected_now.replace(tzinfo=None)

    monkeypatch.setattr(manager, "datetime", Clock)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = runtime.receiver, runtime
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": runtime})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        "Idempotency-Key": "paused-workflow",
        **runtime.receiver.identity.headers(),
    }
    observations = {"reason": reason, "phase": phase, "cause": cause}

    def save():
        observations["commands"] = commands

    async def guide(key):
        async with factory() as session:
            await _controls(session).guide_workspace(
                "ws_cloud_stable",
                directive="Continue without changing protected configuration.",
                idempotency_key=key,
            )
            await session.commit()

    async def row_state():
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            return {
                "status": row.status,
                "failure_reason": row.failure_reason,
                "failure_message": row.failure_message,
                "claim": row.execution_claimed_by,
                "epoch": row.execution_claim_epoch,
                "block_reason": row.block_reason_code,
                "policy": row.task_policy,
            }

    async def await_task_boundary(event):
        waiter = asyncio.create_task(event.wait())
        try:
            tasks = list(built.worker._execution_tasks.values())
            assert tasks
            # This is a lifecycle checkpoint, not a throughput assertion. The
            # shared Postgres-test watchdog bounds slow or stuck workers.
            await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/v1/workspaces", json=payload, headers=headers)
            assert created.status_code == 202, created.text
            await built.worker.run_once()
            if phase == "initial_pause":
                await await_task_boundary(entered)
                assert entered.is_set(), await row_state()
            else:
                await built.worker.wait_for_execution_tasks()
            observations["initial"] = await row_state()
            assert observations["initial"]["status"] == reason, observations
            assert warm, commands
            if reason == "blocked":
                guided = await client.post(
                    "/v1/workspaces/ws_cloud_stable/guide",
                    json={"directive": "Continue without changing protected configuration."},
                    headers={
                        **headers,
                        "Authorization": "Bearer synthetic-operator",
                        "Idempotency-Key": "guide-paused",
                    },
                )
                assert (
                    guided.status_code == 403
                    and guided.json()["error_code"] == "HOSTED_RUNTIME_INTEGRATION_REQUIRED"
                ), guided.text
                observations["guide_http"] = (
                    "403 HOSTED_RUNTIME_INTEGRATION_REQUIRED; conditional existing internal-service resume only"
                )
                # Test the existing paused-resume lifecycle without opening the
                # hosted HTTP guard. This is not proof of a public hosted guide.
                if phase != "initial_pause":
                    await guide("canonical-service-guide")
            elif phase != "initial_pause":
                Clock.selected_now = datetime.fromisoformat(
                    observations["initial"]["policy"]["provider_recovery_state"]["not_before"]
                ) + timedelta(seconds=1)
            if phase != "initial_pause":
                stage = "resuming"
                await built.worker.run_once()
                await await_task_boundary(entered)
                assert entered.is_set(), await row_state()
            observations["at_cancel"] = await row_state()
            if cause in {"revoke", "expiry", "revoke_overlap"}:
                overlap_armed = cause == "revoke_overlap"
                await deny_agent(
                    runtime, payload, "revoke" if overlap_armed else cause, monkeypatch
                )
                if overlap_armed:
                    await asyncio.wait_for(final_check_entered.wait(), 10)
                    built.worker._execution_tasks["ws_cloud_stable"].cancel()
                    hold.set()
            else:
                built.worker._execution_tasks["ws_cloud_stable"].cancel()
            await built.worker.wait_for_execution_tasks()
            observations["after_cancel"] = await row_state()
            observations["warm_after_cancel"] = warm
            observations["view_after_cancel"] = (
                runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
            ).exists()
            if cause == "cleanup_failure":
                assert observations["after_cancel"]["status"] == "failed", observations
                assert not warm
                # A failed-but-retained workspace is not an inactive binding.
                # Its still-current view is valid until revoke/expiry or GC.
                await runtime.workspace("ws_cloud_stable")
                await deny_agent(runtime, payload, "revoke", monkeypatch)
                await scope.admission_check()
                observations["view_after_revoke_sweep"] = (
                    runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
                ).exists()
                save()
                assert not observations["view_after_revoke_sweep"]
                return
            if cause == "revoke_overlap":
                overlap_armed = False
                await scope.admission_check()
                observations["after_sweep"] = await row_state()
                observations["warm_after_sweep"] = warm
                observations["view_after_sweep"] = (
                    runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
                ).exists()
                save()
                assert (
                    not observations["warm_after_cancel"] and not observations["view_after_cancel"]
                ), observations
                return
            if cause != "authorized_cancel":
                save()
                assert observations["after_cancel"]["status"] == "failed", observations
                assert not warm
                assert not (
                    runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
                ).exists()
                return
            if phase == "advanced":
                save()
                assert observations["at_cancel"]["status"] == "pushing", observations
                assert observations["after_cancel"]["status"] == "pushing", observations
                assert warm
                return
            assert observations["after_cancel"]["status"] == reason, observations
            if reason == "blocked" and phase in {"after_pause", "initial_pause"}:
                await guide("canonical-service-guide-after-repause")
            if reason == "recovering":
                Clock.selected_now = datetime.fromisoformat(
                    observations["after_cancel"]["policy"]["provider_recovery_state"]["not_before"]
                ) + timedelta(seconds=1)
            stage = "retry"
            await built.worker.run_once()
            if built.worker._execution_tasks:
                await await_task_boundary(retry_entered)
            observations["next_resume"] = await row_state()
            observations["next_agent_entered"] = retry_entered.is_set()
            save()
            assert observations["warm_after_cancel"], observations
            assert retry_entered.is_set(), observations
    finally:
        for task in built.worker._execution_tasks.values():
            if not task.done():
                task.cancel()
        await built.worker.wait_for_execution_tasks()
        await built.engine.dispose()
