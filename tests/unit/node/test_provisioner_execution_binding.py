"""Repository profile resolution binds all connections before stack materialization."""

import pytest
import yaml

from awf.common.agent_execution import bind_execution_profile, execution_from_policy
from awf.db.repositories import WorkspaceRepository
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    _git,
    git_manager,
    origin_repo,
    session_factory,
)
from tests.unit.service.test_agent_execution_snapshot import freeze_execution_policy, request

_IMPORTED_FIXTURES = (git_manager, origin_repo, session_factory)


@pytest.mark.parametrize("provider", ["native", "ollama", "openrouter"])
async def test_auto_profile_binds_primary_and_fallback_before_launch(
    origin_repo, git_manager, session_factory, monkeypatch, provider
):
    profile = {
        "name": "repo-provider",
        "runtime": {
            "environment": {
                "OLLAMA_HOST": "http://model-sidecar:12345/prefix/v1",
                "OPENROUTER_API_KEY": "${REPO_ROUTER_KEY}",
            }
        },
        "services": [{"name": "model-sidecar", "image": "ollama/ollama:test"}],
    }
    (origin_repo / ".awf").mkdir()
    (origin_repo / ".awf/workspace.yml").write_text(yaml.safe_dump(profile))
    _git(["add", ".awf/workspace.yml"], origin_repo)
    _git(["commit", "-m", "repo profile"], origin_repo)
    payload = request(
        provider=None if provider == "native" else provider,
        model="coder",
        provider_recovery={
            "fallbacks": [
                {"agent": "claude_code", "provider": "ollama", "model": "fallback"},
                {"agent": "codex", "provider": "openrouter", "model": "router/fallback"},
            ]
        },
    )
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile=None, enabled=True, environ={})
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="provider",
            task_prompt="fix",
            agent="codex",
            test_commands=[],
            profile_ref="auto",
            task_policy=policy,
        )
        await session.commit()
        workspace_id = ws.id
    requests = []

    class Launcher:
        async def launch(self, launch):
            requests.append(launch)
            async with session_factory() as session:
                persisted = await WorkspaceRepository(session).get(workspace_id)
                assert execution_from_policy(persisted.task_policy) == launch.execution
            return

    monkeypatch.setenv("OLLAMA_HOST", "http://wrong-service-default:11434")
    await Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node"),
        stack_launcher=Launcher(),
    ).provision(workspace_id)
    assert len(requests) == 1
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == "ready"
        assert "agent_execution_profile_pending" not in ws.task_policy
        primary = execution_from_policy(ws.task_policy)
        daemon, router = [
            execution_from_policy(t) for t in ws.task_policy["provider_recovery"]["fallbacks"]
        ]
        assert daemon.endpoint == "http://model-sidecar:12345/prefix"
        assert daemon.network_service == "model-sidecar"
        assert router.credential_ref == "env/REPO_ROUTER_KEY"
        if provider == "native":
            assert primary.provider == "openai"
            assert primary.endpoint is None
        else:
            assert primary.endpoint == (
                daemon.endpoint if provider == "ollama" else router.endpoint
            )
        assert primary.credential_ref == (
            router.credential_ref if provider == "openrouter" else None
        )
        assert primary.network_service == ("model-sidecar" if provider == "ollama" else None)

        assert (
            bind_execution_profile(
                ws.task_policy,
                agent="codex",
                profile={},
                environ={"OLLAMA_HOST": "http://later-changed:11434"},
            )
            == ws.task_policy
        )
