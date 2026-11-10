"""One-shot diagnostic execution with durable fencing and trusted verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentRunError, get_adapter
from awf.adapters.diagnostic_events import coding_witness
from awf.adapters.provider_config import RUNTIME_PYTHON_PATH
from awf.common.agent_execution import execution_from_policy
from awf.control.executor.work_slot_binding import work_slot_for
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.runtime.provider_diagnostic_fixture import FIXTURE_VERSION, PROMPT, RUNTIME_FIXTURE_PATH
from awf.service.provider_diagnostics import DIAGNOSTIC_KEY, DIAGNOSTIC_KIND


def compose_args(ws: Any, compose_file: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        ws.compose_project_name or "awf_" + ws.id,
        "-f",
        str(compose_file),
    ]


async def runtime_identity(executor: Any, ws: Any, compose_file: Path) -> dict[str, Any]:
    prefix = compose_args(ws, compose_file)
    capability = await executor._runner.run(
        [
            *prefix,
            "exec",
            "-T",
            "agent",
            RUNTIME_PYTHON_PATH,
            "-I",
            "-S",
            RUNTIME_FIXTURE_PATH,
            "--capability",
        ],
        timeout_seconds=10,
    )
    if capability.returncode or capability.stdout.strip() != FIXTURE_VERSION:
        raise ValueError("PROVIDER_DIAGNOSTIC_RUNTIME_UNSUPPORTED")
    cli = "codex" if ws.agent == "codex" else "claude"
    version = await executor._runner.run(
        [*prefix, "exec", "-T", "agent", cli, "--version"], timeout_seconds=10
    )
    container = await executor._runner.run([*prefix, "ps", "-q", "agent"], timeout_seconds=10)
    if version.returncode or container.returncode or not container.stdout.strip():
        raise ValueError("PROVIDER_DIAGNOSTIC_RUNTIME_UNSUPPORTED")
    image = await executor._runner.run(
        ["docker", "inspect", "--format", "{{.Image}}", container.stdout.strip()],
        timeout_seconds=10,
    )
    if image.returncode or not image.stdout.strip().startswith("sha256:"):
        raise ValueError("PROVIDER_DIAGNOSTIC_RUNTIME_UNSUPPORTED")
    selected = execution_from_policy(ws.task_policy) if hasattr(ws, "task_policy") else None
    probe: dict[str, Any] = {}
    if selected is not None and selected.third_party:
        from awf.adapters.provider_config import RUNTIME_CONFIG_PATH

        health = await executor._runner.run(
            [
                *prefix,
                "exec",
                "-T",
                "agent",
                RUNTIME_PYTHON_PATH,
                "-I",
                "-S",
                RUNTIME_CONFIG_PATH,
                json.dumps(selected.model_dump(mode="json")),
                "--probe-json",
            ],
            timeout_seconds=12,
        )
        try:
            probe = json.loads(health.stdout)
        except ValueError:
            raise ValueError("PROVIDER_RESPONSE_INVALID") from None
        if health.returncode or not isinstance(probe, dict) or probe.get("ok") is not True:
            raise ValueError("PROVIDER_ENDPOINT_UNREACHABLE")
    return {
        "endpoint_health": "reachable" if probe else "not_probed_native",
        "model_version": probe.get("model_version"),
        "cli_version": version.stdout.strip()[:200],
        "runtime_image": image.stdout.strip(),
        "fixture_version": FIXTURE_VERSION,
    }


async def finish_diagnostic(executor: Any, ws: Any, result: dict[str, Any]) -> bool:
    async with executor._session_factory() as session:
        repo = WorkspaceRepository(session)
        current = await repo.get_for_update(ws.id)
        if (
            current is None
            or current.status not in {"running", "validating"}
            or current.execution_claimed_by != ws.execution_claimed_by
            or current.execution_claim_epoch != ws.execution_claim_epoch
        ):
            return False
        policy = dict(current.task_policy or {})
        diagnostic = dict(policy[DIAGNOSTIC_KEY])
        diagnostic["result"] = result
        diagnostic["state"] = result["outcome"]
        policy[DIAGNOSTIC_KEY] = diagnostic
        current.task_policy = policy
        success = result["outcome"] == "passed"
        if success and current.status == "running":
            await repo.transition(
                current, to=WorkspaceStatus.validating, reason_code="PROVIDER_DIAGNOSTIC_VERIFYING"
            )
        if not success:
            current.failure_reason = FailureReason.agent_failure.value
        await repo.transition(
            current,
            to=WorkspaceStatus.completed if success else WorkspaceStatus.failed,
            reason_code=str(result["reason_code"]),
            payload={"provider_diagnostic": result},
        )
        for operation in await OperationRepository(session).list_for_workspace(
            ws.id, operation_type=OperationType.start
        ):
            if (operation.payload or {}).get("source") == DIAGNOSTIC_KIND and operation.status in {
                "pending",
                "running",
            }:
                await OperationRepository(session).finish(
                    operation,
                    status=OperationStatus.succeeded if success else OperationStatus.failed,
                    result=result,
                )
        current.execution_claimed_by = None
        current.execution_claim_expires_at = None
        await session.commit()
        return True


async def persist_runtime_identity(executor: Any, ws: Any, identity: dict[str, Any]) -> bool:
    """Retain inspected identity and recheck cancellation before the paid call."""
    async with executor._session_factory() as session:
        repo = WorkspaceRepository(session)
        current = await repo.get_for_update(ws.id)
        if (
            current is None
            or current.status != "running"
            or current.execution_claimed_by != ws.execution_claimed_by
            or current.execution_claim_epoch != ws.execution_claim_epoch
        ):
            return False
        diagnostic = dict(current.task_policy[DIAGNOSTIC_KEY])
        diagnostic["runtime_identity"] = identity
        current.task_policy = {**current.task_policy, DIAGNOSTIC_KEY: diagnostic}
        await repo.add_event(
            current,
            event_type="workspace.provider_diagnostic_runtime",
            reason_code="PROVIDER_DIAGNOSTIC_READY",
            payload=identity,
        )
        await session.commit()
        return True


async def execute_diagnostic(executor: Any, ws: Any, *, compose_file: Path) -> None:
    # A row lock protects at-most-once launch even if two execute callbacks race.
    async with executor._session_factory() as session:
        repo = WorkspaceRepository(session)
        current = await repo.get_for_update(ws.id)
        if (
            current is None
            or current.status != "running"
            or current.execution_claimed_by != ws.execution_claimed_by
            or current.execution_claim_epoch != ws.execution_claim_epoch
        ):
            return
        diagnostic = dict((current.task_policy or {})[DIAGNOSTIC_KEY])
        if diagnostic.get("invocation_started_at"):
            return  # Recovery settles interrupted work; never replay a paid call.
        diagnostic["invocation_started_at"] = datetime.now(UTC).isoformat()
        diagnostic["state"] = "running"
        current.task_policy = {**(current.task_policy or {}), DIAGNOSTIC_KEY: diagnostic}
        for op in await OperationRepository(session).list_for_workspace(
            ws.id, operation_type=OperationType.start
        ):
            if (op.payload or {}).get("source") == DIAGNOSTIC_KIND and op.status == "pending":
                op.status = "running"
                op.started_at = datetime.now(UTC)
        await session.commit()
    selected = execution_from_policy(ws.task_policy)
    assert selected is not None  # creation freezes the required contract
    result: dict[str, Any] = {
        "outcome": "inconclusive",
        "reason_code": "PROVIDER_DIAGNOSTIC_EVIDENCE_MISSING",
        "selection": selected.model_dump(mode="json"),
        "connection_scope": selected.connection_scope(ws.id),
        "fixture_version": FIXTURE_VERSION,
        "endpoint_health": "unknown",
        "protocol_evidence": "inconclusive",
        "fixture_verification": False,
    }
    try:
        from awf.service.provider_diagnostics import require_diagnostic_contract

        require_diagnostic_contract(ws)
        deadline = diagnostic["request"]["deadline_seconds"]
        async with asyncio.timeout(deadline):
            identity = await runtime_identity(executor, ws, compose_file)
            result.update(identity)
            if not await persist_runtime_identity(executor, ws, identity):
                return
            adapter = get_adapter(
                ws.agent,
                runner=executor._runner,
                execution=selected,
                log_store=executor._log_store,
                agent_wall_timeout_seconds=deadline,
                agent_idle_timeout_seconds=deadline,
            )
            adapter.diagnostic_events = True
            run = await adapter.run(
                compose_project=ws.compose_project_name or "awf_" + ws.id,
                compose_file=compose_file,
                prompt=PROMPT,
                workspace_id=ws.id,
                log_source="provider_diagnostic",
                worktree_path=executor._config.worktrees_root / ws.id,
                work_slot=work_slot_for(executor, ws.id),
            )
            proof = coding_witness(ws.agent, run.stdout, prompt=PROMPT)
            result["protocol_evidence"] = proof.status
            result["witness"] = {
                "tool_ids": list(proof.tool_ids),
                "events": list(proof.events),
                "transcript_sha256": hashlib.sha256(run.stdout.encode()).hexdigest(),
            }
            if proof.status == "passed" and proof.nonce:
                verified = await executor._runner.run(
                    [
                        *compose_args(ws, compose_file),
                        "exec",
                        "-T",
                        "agent",
                        RUNTIME_PYTHON_PATH,
                        "-I",
                        "-S",
                        RUNTIME_FIXTURE_PATH,
                        "verify",
                        "/workspace",
                        proof.nonce,
                    ],
                    timeout_seconds=10,
                )
                result["fixture_verification"] = verified.returncode == 0
                result["outcome"] = "passed" if verified.returncode == 0 else "failed"
                result["reason_code"] = (
                    "PROVIDER_DIAGNOSTIC_PASSED"
                    if verified.returncode == 0
                    else "PROVIDER_DIAGNOSTIC_FIXTURE_FAILED"
                )
    except asyncio.CancelledError:
        # Adapter cancellation drains/kills its tracked invocation first. Recovery
        # owns any unfinished settlement, never a replacement agent invocation.
        raise
    except (AgentRunError, TimeoutError, ValueError, OSError) as error:
        result["execution_reason_code"] = (
            error.reason_code
            if isinstance(error, AgentRunError)
            else str(error)
            if str(error).isupper() and len(str(error)) < 100
            else type(error).__name__
        )
        result["reason_code"] = (
            "PROVIDER_DIAGNOSTIC_TIMEOUT"
            if isinstance(error, TimeoutError)
            else "PROVIDER_DIAGNOSTIC_EXECUTION_FAILED"
        )
    await finish_diagnostic(executor, ws, result)
