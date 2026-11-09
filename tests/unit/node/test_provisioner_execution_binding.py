"""Repository profile resolution binds all connections before stack materialization."""

import asyncio

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
@pytest.mark.parametrize(
    "interruption",
    [
        None,
        "error",
        "worker_loss",
        "before_commit",
        "key_alias_only",
        "worker_loss_same_profile",
        "fenced",
        "port_conflict",
    ],
)
async def test_auto_profile_binds_primary_and_fallback_before_launch(
    origin_repo, git_manager, session_factory, monkeypatch, provider, interruption
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
    if interruption == "port_conflict":
        profile["services"][0]["ports"] = [[11434, 18080]]
    if interruption == "worker_loss_same_profile":
        profile["runtime"]["environment"].pop("OLLAMA_HOST")
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

    monkeypatch.delenv("AWF_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("AWF_OPENCODE_OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://wrong-service-default:11434")
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node"),
        stack_launcher=Launcher(),
    )
    if interruption in {"fenced", "port_conflict"}:
        if interruption == "fenced":
            check_ports = provisioner._check_auto_resolved_profile_host_ports

            async def transfer_claim(**kwargs):
                async with session_factory() as session:
                    row = await WorkspaceRepository(session).get(workspace_id)
                    row.execution_claim_epoch += 1
                    await session.commit()
                return await check_ports(**kwargs)

            async with session_factory() as session:
                row = await WorkspaceRepository(session).get(workspace_id)
                row.status = "provisioning"
                await session.commit()
            monkeypatch.setattr(
                provisioner, "_check_auto_resolved_profile_host_ports", transfer_claim
            )
            await provisioner.provision_claimed(
                workspace_id, execution_claim_epoch=ws.execution_claim_epoch
            )
        else:
            async with session_factory() as session:
                blocker = await WorkspaceRepository(session).create(
                    repo_url=str(origin_repo),
                    branch_base="development",
                    task_title="port owner",
                    task_prompt="fix",
                    agent="codex",
                    test_commands=[],
                    resolved_profile=profile,
                )
                blocker.status = "ready"
                await session.commit()
            await provisioner.provision(workspace_id)
        assert requests == []
        async with session_factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            assert row.task_policy == policy
            if interruption == "fenced":
                assert row.status == "provisioning"
                assert row.resolved_profile is None
            else:
                from awf.db.repositories import WorkspaceEventRepository

                assert row.status == "failed"
                events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
                assert any(
                    event.reason_code == "AUTO_PROFILE_HOST_PORT_CHECK_FATAL" for event in events
                )
        return
    if interruption:
        if interruption == "before_commit":
            advance_version = WorkspaceRepository.advance_workspace_version

            async def interrupt_before_commit(repo, row):
                result = await advance_version(repo, row)
                if "agent_execution_profile_pending" not in row.task_policy:
                    raise asyncio.CancelledError("worker lost before binding commit")
                return result

            monkeypatch.setattr(
                WorkspaceRepository, "advance_workspace_version", interrupt_before_commit
            )
        else:

            async def interrupt_after_binding(**_kwargs):
                if interruption == "error":
                    raise RuntimeError("failure after binding")
                raise asyncio.CancelledError("worker lost after binding")

            monkeypatch.setattr(
                provisioner, "_run_deferred_cursor_auto_router_preflight", interrupt_after_binding
            )
        with pytest.raises(RuntimeError if interruption == "error" else asyncio.CancelledError):
            await provisioner.provision(workspace_id)
        assert requests == []
        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)
            if interruption == "before_commit":
                assert persisted.resolved_profile is None
                assert persisted.task_policy == policy
            else:
                assert persisted.resolved_profile["name"] == "repo-provider"
                assert "agent_execution_profile_pending" not in persisted.task_policy
            persisted.status = "provisioning"
            await session.commit()
        # A replacement worker must use either the committed pair or a fresh
        # pair after rollback, even if the repository profile changed meanwhile.
        if interruption not in {"key_alias_only", "worker_loss_same_profile"}:
            profile["name"] = "changed-profile"
            profile["runtime"]["environment"]["OLLAMA_HOST"] = (
                "http://changed-sidecar:23456/changed/v1"
            )
            profile["services"][0]["name"] = "changed-sidecar"
        if interruption != "worker_loss_same_profile":
            profile["runtime"]["environment"]["OPENROUTER_API_KEY"] = "${CHANGED_ROUTER_KEY}"
        (git_manager.get_worktree_path(workspace_id) / ".awf/workspace.yml").write_text(
            yaml.safe_dump(profile)
        )
        if interruption == "before_commit":
            monkeypatch.setattr(WorkspaceRepository, "advance_workspace_version", advance_version)
        if interruption == "worker_loss_same_profile":
            monkeypatch.setenv("OLLAMA_HOST", "http://changed-worker-default:11434")
        monkeypatch.setattr(git_manager, "add_worktree", git_manager.ensure_worktree)
        replacement = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            config=ProvisionerConfig(node_id="replacement"),
            stack_launcher=Launcher(),
        )
        if interruption in {"error", "worker_loss", "key_alias_only"}:
            from awf.profiles.resolver import ProfileResolutionError

            with pytest.raises(ProfileResolutionError) as failure:
                await replacement.provision_claimed(workspace_id)
            assert failure.value.reason_code == "EXECUTION_PROFILE_CHANGED"
            assert requests == []
            async with session_factory() as session:
                persisted = await WorkspaceRepository(session).get(workspace_id)
                assert persisted.status == "failed"
                assert persisted.resolved_profile["name"] == "repo-provider"
                fallback = execution_from_policy(
                    persisted.task_policy["provider_recovery"]["fallbacks"][1]
                )
                assert fallback.credential_ref == "env/REPO_ROUTER_KEY"
            return
        await replacement.provision_claimed(workspace_id)
    else:
        await provisioner.provision(workspace_id)
    assert len(requests) == 1
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws.status == "ready"
        assert "agent_execution_profile_pending" not in ws.task_policy
        primary = execution_from_policy(ws.task_policy)
        daemon, router = [
            execution_from_policy(t) for t in ws.task_policy["provider_recovery"]["fallbacks"]
        ]
        rolled_back = interruption == "before_commit"
        assert ws.resolved_profile["name"] == (
            "changed-profile" if rolled_back else "repo-provider"
        )
        assert requests[0].profile.name == ws.resolved_profile["name"]
        assert daemon.endpoint == (
            "http://wrong-service-default:11434"
            if interruption == "worker_loss_same_profile"
            else "http://changed-sidecar:23456/changed"
            if rolled_back
            else "http://model-sidecar:12345/prefix"
        )
        assert daemon.network_service == (
            None
            if interruption == "worker_loss_same_profile"
            else "changed-sidecar"
            if rolled_back
            else "model-sidecar"
        )
        assert router.credential_ref == (
            "env/CHANGED_ROUTER_KEY" if rolled_back else "env/REPO_ROUTER_KEY"
        )
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
        assert primary.network_service == (daemon.network_service if provider == "ollama" else None)

        assert (
            bind_execution_profile(
                ws.task_policy,
                agent="codex",
                profile={},
                environ={"OLLAMA_HOST": "http://later-changed:11434"},
            )
            == ws.task_policy
        )


@pytest.fixture
async def pending_binding_workspace(session_factory):
    payload = request(
        provider="ollama",
        model="coder",
        provider_recovery={
            "fallbacks": [{"agent": "codex", "provider": "openrouter", "model": "router/coder"}]
        },
    )
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile=None, enabled=True, environ={})
    async with session_factory() as session:
        row = await WorkspaceRepository(session).create(
            repo_url="https://example.com/repo",
            branch_base="development",
            task_title="atomic profile binding",
            task_prompt="fix",
            agent="codex",
            test_commands=[],
            task_policy=policy,
        )
        row.status = "provisioning"
        await session.commit()
        return row


@pytest.mark.parametrize("changed", ["missing", "status", "epoch"])
async def test_atomic_binding_rejects_stale_provisioner(
    session_factory, git_manager, pending_binding_workspace, changed
):
    from awf.profiles.models import WorkspaceProfile

    row = pending_binding_workspace
    async with session_factory() as session:
        persisted = await WorkspaceRepository(session).get(row.id)
        if changed == "missing":
            await session.delete(persisted)
        elif changed == "status":
            persisted.status = "cancelled"
        else:
            persisted.execution_claim_epoch += 1
        await session.commit()
    provisioner = Provisioner(
        session_factory=session_factory, git=git_manager, config=ProvisionerConfig(node_id="node")
    )
    assert (
        await provisioner._check_auto_resolved_profile_host_ports(
            workspace_id=row.id,
            profile=WorkspaceProfile(name="stale"),
            task_policy=row.task_policy,
            execution_claim_epoch=row.execution_claim_epoch,
            bind_execution=True,
        )
        is None
    )
    async with session_factory() as session:
        persisted = await WorkspaceRepository(session).get(row.id)
        if persisted is not None:
            assert persisted.task_policy == row.task_policy
            assert persisted.resolved_profile is None


@pytest.mark.parametrize("mode", ["same_profile", "changed_profile", "other_workspace"])
async def test_concurrent_binding_preserves_profile_winner_and_port_admission(
    session_factory, git_manager, pending_binding_workspace, mode
):
    from awf.profiles.models import WorkspaceProfile
    from awf.service.workspaces import WorkspaceCreateHostPortConflictError

    row = pending_binding_workspace
    other_id = row.id
    if mode == "other_workspace":
        async with session_factory() as session:
            other = await WorkspaceRepository(session).create(
                repo_url=row.repo_url,
                branch_base="development",
                task_title="competitor",
                task_prompt="fix",
                agent=row.agent,
                test_commands=[],
                task_policy=row.task_policy,
            )
            other.status = "provisioning"
            await session.commit()
            other_id = other.id
    provisioner = Provisioner(
        session_factory=session_factory, git=git_manager, config=ProvisionerConfig(node_id="node")
    )

    async def bind(workspace_id, name):
        profile = WorkspaceProfile.model_validate(
            {
                "name": name,
                "runtime": {
                    "environment": {
                        "OLLAMA_HOST": f"http://{name}:11434",
                        "OPENROUTER_API_KEY": "${" + name.upper() + "_KEY}",
                    }
                },
                "services": [
                    {"name": name, "image": "ollama/ollama:test", "ports": [[11434, 18080]]}
                ],
            }
        )
        return await provisioner._check_auto_resolved_profile_host_ports(
            workspace_id=workspace_id,
            profile=profile,
            excluding_workspace_id=workspace_id,
            task_policy=row.task_policy,
            execution_claim_epoch=row.execution_claim_epoch,
            bind_execution=True,
        )

    results = await asyncio.gather(
        bind(row.id, "first"),
        bind(other_id, "first" if mode == "same_profile" else "second"),
        return_exceptions=True,
    )
    if mode == "same_profile":
        assert results[0] == results[1]
        assert not isinstance(results[0], BaseException)
    elif mode == "changed_profile":
        assert results.count(None) == 1
        assert not any(isinstance(result, BaseException) for result in results)
    else:
        assert (
            sum(isinstance(result, WorkspaceCreateHostPortConflictError) for result in results) == 1
        )
    for workspace_id in {row.id, other_id}:
        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)
            assert persisted.status == "provisioning"
            if persisted.resolved_profile is None:
                assert persisted.task_policy == row.task_policy
                continue
            winner = persisted.resolved_profile["name"]
            selected = execution_from_policy(persisted.task_policy)
            fallback = execution_from_policy(
                persisted.task_policy["provider_recovery"]["fallbacks"][0]
            )
            assert selected.endpoint == f"http://{winner}:11434"
            assert selected.network_service == winner
            assert fallback.credential_ref == f"env/{winner.upper()}_KEY"


async def test_pending_connection_binding_reuses_previously_frozen_profile(
    session_factory, git_manager, pending_binding_workspace
):
    from awf.profiles.models import WorkspaceProfile

    row = pending_binding_workspace
    frozen = WorkspaceProfile.model_validate(
        {
            "name": "already-frozen",
            "runtime": {"environment": {"OLLAMA_HOST": "http://frozen:11434"}},
        }
    )
    async with session_factory() as session:
        persisted = await WorkspaceRepository(session).get(row.id)
        persisted.resolved_profile = frozen.model_dump(mode="json", by_alias=True)
        await session.commit()
    provisioner = Provisioner(
        session_factory=session_factory, git=git_manager, config=ProvisionerConfig(node_id="node")
    )
    result = await provisioner._check_auto_resolved_profile_host_ports(
        workspace_id=row.id,
        profile=WorkspaceProfile(name="late-resolver"),
        task_policy=row.task_policy,
        execution_claim_epoch=row.execution_claim_epoch,
        bind_execution=True,
    )
    profile, policy = result
    assert profile == frozen
    assert execution_from_policy(policy).endpoint == "http://frozen:11434"
    async with session_factory() as session:
        persisted = await WorkspaceRepository(session).get(row.id)
        assert persisted.resolved_profile["name"] == "already-frozen"
        assert persisted.task_policy == policy


@pytest.mark.parametrize("frozen_selection", [False, True])
async def test_native_profile_rehydration_preserves_existing_resolution_behavior(
    session_factory, git_manager, origin_repo, frozen_selection
):
    from awf.common.audit import redact_audit_value
    from awf.profiles.models import WorkspaceProfile

    policy = (
        freeze_execution_policy(request(), profile={}, enabled=False, environ={})
        if frozen_selection
        else {}
    )
    async with session_factory() as session:
        row = await WorkspaceRepository(session).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="native retry",
            task_prompt="fix",
            agent="codex",
            test_commands=[],
            task_policy=policy,
            requested_profile={"name": "rehydrated-native"},
            resolved_profile=redact_audit_value(
                WorkspaceProfile(name="previous-native").model_dump(mode="json", by_alias=True)
            ),
        )
        row.status = "provisioning"
        await session.commit()
        workspace_id = row.id
    await Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node"),
    ).provision_claimed(workspace_id)
    async with session_factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == "ready"
        assert row.resolved_profile["name"] == "rehydrated-native"
        assert row.task_policy == policy
