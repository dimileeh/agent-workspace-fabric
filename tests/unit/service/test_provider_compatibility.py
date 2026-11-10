"""Catalog/health cannot replace exact runtime coding-cycle evidence."""

from awf.common.agent_execution import require_execution_admission, resolve_execution
from awf.service.provider_compatibility import exact_compatibility


def test_evidence_is_exact_and_never_catalog_claims():
    selected = resolve_execution(
        agent="codex", provider="openrouter", model="candidate", allow_unverified=True
    )
    identity = {
        "runtime_image": "sha256:actual",
        "cli_version": "codex 0.162.0",
        "fixture_version": "coding-cycle-v1",
        "model_version": "digest",
    }
    result = {
        "outcome": "passed",
        "selection": selected.model_dump(mode="json"),
        "connection_scope": selected.connection_scope(),
        "protocol_evidence": "passed",
        "fixture_verification": True,
        **identity,
    }
    assert exact_compatibility(selected, result, identity=identity)
    for field in identity:
        assert not exact_compatibility(selected, result, identity={**identity, field: "changed"})
    assert not exact_compatibility(
        selected, {**result, "protocol_evidence": "provider_claim"}, identity=identity
    )
    assert not exact_compatibility(
        selected.model_copy(update={"credential_ref": "env/OTHER"}), result, identity=identity
    )
    assert not exact_compatibility(
        selected, {**result, "outcome": "inconclusive"}, identity=identity
    )


def test_enabled_selection_reaches_runtime_network_probe():
    selected = resolve_execution(
        agent="codex", provider="ollama", model="candidate", allow_unverified=True
    )
    require_execution_admission(selected, enabled=True)


async def test_successful_exact_diagnostic_admits_ordinary_work_and_changed_endpoint_does_not(
    engine, tmp_path
):
    import pytest

    from awf.api.schemas import WorkspaceCreateRequest
    from awf.common.config import Settings
    from awf.db.repositories import WorkspaceRepository
    from awf.service.workspaces import WorkspaceProviderReadinessBlockedError, WorkspaceService
    from tests.unit.control.test_provider_diagnostic_execution import prepared

    factory, response, executor = await prepared(engine, tmp_path)
    selected = resolve_execution(
        agent="codex", provider="ollama", model="candidate:cloud", allow_unverified=True
    )
    payload = WorkspaceCreateRequest.model_validate(
        {
            "repo": {"url": "https://github.com/example/project"},
            "task": {
                "title": "coding",
                "prompt": "work",
                "provider": "ollama",
                "model": "candidate:cloud",
            },
            "workspace": {"profile": {"name": "generic", "docker": {"mode": "none"}}},
        }
    )
    service = WorkspaceService(
        factory, settings=Settings(_env_file=None, third_party_agent_providers_enabled=True)
    )
    with pytest.raises(WorkspaceProviderReadinessBlockedError):
        await service.create(payload)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        ws.status = "completed"
        ws.task_policy = {
            **ws.task_policy,
            "provider_diagnostic": {
                **ws.task_policy["provider_diagnostic"],
                "result": {
                    "outcome": "passed",
                    "selection": selected.model_dump(mode="json"),
                    "connection_scope": selected.connection_scope(),
                    "protocol_evidence": "passed",
                    "fixture_verification": True,
                    "runtime_image": "sha256:actual",
                    "cli_version": "codex 0.162.0",
                    "fixture_version": "coding-cycle-v1",
                    "model_version": None,
                },
            },
        }
        await session.commit()
    accepted = await service.create(payload)
    assert accepted.task_policy["require_compatibility_evidence"]
    assert accepted.task_policy["coding_compatibility_evidence"]["outcome"] == "passed"
    payload.workspace.profile.runtime.environment["AWF_OLLAMA_BASE_URL"] = (
        "http://independent:11434"
    )
    with pytest.raises(WorkspaceProviderReadinessBlockedError):
        await service.create(payload)


async def test_runtime_guard_checks_actual_image_cli_and_model(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from awf.common.commands import CommandResult
    from awf.service.provider_compatibility import guard_runtime_compatibility

    selected = resolve_execution(
        agent="codex", provider="openrouter", model="candidate", allow_unverified=True
    )
    identity = {
        "runtime_image": "sha256:actual",
        "cli_version": "codex 0.162.0",
        "fixture_version": "coding-cycle-v1",
        "model_version": None,
    }
    evidence = {
        "outcome": "passed",
        "selection": selected.model_dump(mode="json"),
        "connection_scope": selected.connection_scope(),
        "protocol_evidence": "passed",
        "fixture_verification": True,
        **identity,
    }
    adapter = SimpleNamespace(
        require_compatibility_evidence=True,
        diagnostic_events=False,
        execution=selected,
        compatibility_evidence=evidence,
        _runner=SimpleNamespace(
            run=AsyncMock(return_value=CommandResult(returncode=0, stdout='{"ok":true}', stderr=""))
        ),
    )
    runtime = AsyncMock(return_value=identity)
    monkeypatch.setattr("awf.control.executor.provider_diagnostics.runtime_identity", runtime)
    await guard_runtime_compatibility(
        adapter, compose_project="awf_ws", compose_file=tmp_path / "compose.yml", workspace_id="ws"
    )
    runtime.return_value = {**identity, "runtime_image": "sha256:changed"}
    with pytest.raises(ValueError, match="MODEL_UNVERIFIED"):
        await guard_runtime_compatibility(
            adapter,
            compose_project="awf_ws",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws",
        )


def test_declared_fallback_retains_its_exact_proof_during_typed_promotion():
    from awf.common.agent_execution import promote_execution
    from awf.service.provider_recovery import FallbackTarget

    selected = resolve_execution(
        agent="codex", provider="openrouter", model="candidate", allow_unverified=True
    )
    evidence = {"outcome": "passed", "selection": selected.model_dump(mode="json")}
    policy = {
        "require_compatibility_evidence": True,
        "provider_recovery": {
            "fallbacks": [
                {
                    "agent_execution": selected.model_dump(mode="json"),
                    "coding_compatibility_evidence": evidence,
                }
            ]
        },
    }
    promoted = promote_execution(
        policy,
        FallbackTarget(selected.agent, selected.provider, selected.model, selected).to_payload(),
    )
    assert promoted["coding_compatibility_evidence"] == evidence
