"""Exact coding evidence; catalog listings and raw endpoint health never qualify."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from awf.common.agent_execution import ExecutionSelection, execution_from_policy
from awf.db.models import Workspace
from awf.runtime.provider_diagnostic_fixture import FIXTURE_VERSION


def exact_compatibility(
    selection: ExecutionSelection,
    evidence: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    workspace_id: str | None = None,
) -> bool:
    return bool(
        evidence.get("outcome") == "passed"
        and evidence.get("protocol_evidence") == "passed"
        and evidence.get("fixture_verification") is True
        and evidence.get("selection") == selection.model_dump(mode="json")
        and evidence.get("connection_scope") == selection.connection_scope(workspace_id)
        and evidence.get("fixture_version") == FIXTURE_VERSION
        and identity.get("runtime_image")
        and identity.get("cli_version")
        and all(
            evidence.get(key) == identity.get(key)
            for key in ("runtime_image", "cli_version", "fixture_version", "model_version")
        )
    )


async def attach_compatibility(session: Any, policy: dict[str, Any]) -> dict[str, Any]:
    if not policy.get("require_compatibility_evidence"):
        return policy
    updated = dict(policy)
    targets = [updated, *updated.get("provider_recovery", {}).get("fallbacks", [])]
    for target in targets:
        selected = execution_from_policy(target)
        if selected is None or not selected.third_party:
            continue
        # Independent sidecars never reuse another workspace namespace's proof.
        if selected.network_service:
            raise ValueError("AGENT_PROVIDER_MODEL_UNVERIFIED")
        rows = (
            await session.scalars(
                select(Workspace.task_policy)
                .where(
                    Workspace.task_kind == "provider_connection_test",
                    Workspace.status == "completed",
                    cast(Workspace.task_policy, JSONB)["provider_diagnostic"]["result"]["selection"]
                    == selected.model_dump(mode="json"),
                )
                .order_by(Workspace.updated_at.desc())
                .limit(20)
            )
        ).all()
        for row in rows:
            evidence = row.get("provider_diagnostic", {}).get("result", {})
            if exact_compatibility(selected, evidence, identity=evidence):
                target["coding_compatibility_evidence"] = evidence
                break
        else:
            raise ValueError("AGENT_PROVIDER_MODEL_UNVERIFIED")
    return updated


async def guard_runtime_compatibility(
    adapter: Any, *, compose_project: str, compose_file: Any, workspace_id: str | None
) -> None:
    if (
        not adapter.require_compatibility_evidence
        or adapter.diagnostic_events
        or adapter.execution is None
        or not adapter.execution.third_party
    ):
        return
    import json
    from types import SimpleNamespace

    from awf.adapters.provider_config import RUNTIME_CONFIG_PATH, RUNTIME_PYTHON_PATH
    from awf.control.executor.provider_diagnostics import compose_args, runtime_identity

    ws = SimpleNamespace(
        id=workspace_id, agent=adapter.execution.agent, compose_project_name=compose_project
    )
    identity = await runtime_identity(SimpleNamespace(_runner=adapter._runner), ws, compose_file)
    health = await adapter._runner.run(
        [
            *compose_args(ws, compose_file),
            "exec",
            "-T",
            "agent",
            RUNTIME_PYTHON_PATH,
            "-I",
            "-S",
            RUNTIME_CONFIG_PATH,
            json.dumps(adapter.execution.model_dump(mode="json")),
            "--probe-json",
        ],
        timeout_seconds=12,
    )
    try:
        probe = json.loads(health.stdout)
    except ValueError:
        raise ValueError("PROVIDER_RESPONSE_INVALID") from None
    if health.returncode or probe.get("ok") is not True:
        raise ValueError("PROVIDER_ENDPOINT_UNREACHABLE")
    identity["model_version"] = probe.get("model_version")
    if not exact_compatibility(
        adapter.execution,
        adapter.compatibility_evidence or {},
        identity=identity,
        workspace_id=workspace_id,
    ):
        raise ValueError("AGENT_PROVIDER_MODEL_UNVERIFIED")
