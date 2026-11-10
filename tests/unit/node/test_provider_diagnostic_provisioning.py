"""Diagnostic provisioning branches before project hooks and forge access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.api.schemas_providers import ProviderConnectionTestRequest
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.service.provider_diagnostics import create_connection_test
from awf.service.workspaces import WorkspaceService
from tests.unit.api.test_provider_connection_tests import capable


@pytest.mark.parametrize(
    "mirror_state,publish_paths",
    [("absent", False), ("empty", False), ("bare", False), ("bare", True)],
)
async def test_early_provisioning_and_managed_fixture(
    engine, tmp_path, mirror_state, publish_paths
):
    from awf.node.provider_diagnostics import _git
    from awf.service.provider_diagnostics import INTERNAL_REPO

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="provision",
    )
    compose_file = tmp_path / "compose.yml"
    launcher = SimpleNamespace(
        launch=AsyncMock(
            return_value=SimpleNamespace(compose_file=compose_file) if publish_paths else None
        )
    )
    before = AsyncMock(side_effect=AssertionError("project preflight"))
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="local"),
        stack_launcher=launcher,
        before_provision=before,
    )
    mirror = provisioner._git._mirror_path(INTERNAL_REPO)
    if mirror_state == "empty":
        mirror.mkdir(parents=True)
    elif mirror_state == "bare":
        await _git("init", "--bare", str(mirror))
    await provisioner.provision(response.workspace_id)
    before.assert_not_awaited()
    request = launcher.launch.call_args.args[0]
    assert request.diagnostic
    assert request.execution.provider == "ollama"
    assert (request.layout.worktree_path / ".git").is_file()
    assert (request.layout.worktree_path / "solution.py").is_file()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == WorkspaceStatus.ready.value
        assert ws.compose_file_path == (str(compose_file) if publish_paths else None)
        assert ws.pr_url is None
    await provisioner._git.remove_worktree(
        workspace_id=response.workspace_id, repo_url=INTERNAL_REPO
    )
    assert not request.layout.worktree_path.exists()


@pytest.mark.parametrize("takeover_point", ["fixture", "claim_check", "launch"])
async def test_replaced_diagnostic_claim_cannot_launch_or_publish_over_new_owner(
    engine, tmp_path, monkeypatch, takeover_point
):
    import awf.node.provider_diagnostics as diagnostics

    factory = await capable(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="claim-takeover",
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(response.workspace_id)
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="test")
        ws.execution_claimed_by = "original-worker"
        ws.execution_claim_epoch = 1
        await session.commit()

    fixture = tmp_path / "worktrees" / response.workspace_id
    replacement_compose = tmp_path / "replacement.yml"

    async def replace_claim():
        async with factory() as session:
            ws = await WorkspaceRepository(session).get_for_update(response.workspace_id)
            ws.execution_claimed_by = "replacement-worker"
            ws.execution_claim_epoch = 2
            ws.branch_name = "replacement-branch"
            ws.compose_project_name = "replacement-project"
            ws.compose_file_path = str(replacement_compose)
            ws.node_id = "replacement-node"
            await session.commit()
        (fixture / "solution.py").write_text("replacement worker edits\n")

    real_fixture = diagnostics.fixture_worktree

    async def fixture_worktree(*args):
        layout = await real_fixture(*args)
        if takeover_point == "fixture":
            await replace_claim()
        return layout

    async def launch(_request):
        await replace_claim()
        return SimpleNamespace(compose_file=tmp_path / "stale.yml")

    launcher = SimpleNamespace(launch=AsyncMock(side_effect=launch))
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="original-node"),
        stack_launcher=launcher,
    )
    real_verify = provisioner._verify_execution_claim_epoch

    async def verify_epoch(*args):
        current = await real_verify(*args)
        if takeover_point == "claim_check":
            assert current
            await replace_claim()
        return current

    monkeypatch.setattr(diagnostics, "fixture_worktree", fixture_worktree)
    monkeypatch.setattr(provisioner, "_verify_execution_claim_epoch", verify_epoch)
    await provisioner.provision_claimed(response.workspace_id, execution_claim_epoch=1)

    assert launcher.launch.await_count == int(takeover_point == "launch")
    assert (fixture / "solution.py").read_text() == "replacement worker edits\n"
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "provisioning"
        assert ws.execution_claimed_by == "replacement-worker"
        assert ws.execution_claim_epoch == 2
        assert ws.branch_name == "replacement-branch"
        assert ws.compose_project_name == "replacement-project"
        assert ws.compose_file_path == str(replacement_compose)
        assert ws.node_id == "replacement-node"
        assert "result" not in ws.task_policy["provider_diagnostic"]


@pytest.mark.parametrize("missing_fixture_ref", [False, True])
async def test_fixture_mirror_repair_preserves_other_workspaces(
    tmp_path, monkeypatch, missing_fixture_ref
):
    import asyncio

    import awf.node.provider_diagnostics as diagnostics

    manager = GitManager(tmp_path)
    existing = await diagnostics.fixture_worktree(manager, "ws_existing")
    solution = existing.worktree_path / "solution.py"
    solution.write_text("independent workspace edits\n")
    mirror = existing.mirror_path
    git_args = ("--git-dir", str(mirror))
    tree = await diagnostics._git(*git_args, "mktree", input_data=b"")
    original = await diagnostics._git(
        *git_args, "commit-tree", tree, input_data=b"preserve healthy fixture ref\n"
    )
    await diagnostics._git(*git_args, "update-ref", "refs/heads/fixture", original)
    await diagnostics._git(*git_args, "pack-refs", "--all")
    existing_head = await diagnostics._git("-C", str(existing.worktree_path), "rev-parse", "HEAD")
    if missing_fixture_ref:
        await diagnostics._git(*git_args, "update-ref", "-d", "refs/heads/fixture")

    independent = await diagnostics.fixture_worktree(GitManager(tmp_path / "other"), "ws_other")
    other_refs = await diagnostics._git("--git-dir", str(independent.mirror_path), "show-ref")
    other_git = (independent.worktree_path / ".git").read_bytes()
    real_git = diagnostics._git
    calls = []
    lock = manager._lock_for_mirror(mirror)

    async def locked_git(*args, **kwargs):
        assert lock.locked()
        calls.append(args)
        return await real_git(*args, **kwargs)

    monkeypatch.setattr(diagnostics, "_git", locked_git)
    async with lock:
        tasks = [
            asyncio.create_task(diagnostics.fixture_worktree(GitManager(tmp_path), workspace_id))
            for workspace_id in ("ws_new_one", "ws_new_two")
        ]
        # Both callers must wait for the already-owned mirror lock before any Git mutation.
        await asyncio.sleep(0)
        assert calls == []
    layouts = await asyncio.gather(*tasks, return_exceptions=True)
    assert not [layout for layout in layouts if isinstance(layout, BaseException)]
    assert sum("commit-tree" in call for call in calls) == int(missing_fixture_ref)
    fixture_head = await real_git(*git_args, "rev-parse", "refs/heads/fixture")
    if not missing_fixture_ref:
        assert fixture_head == original
    for layout in layouts:
        assert layout.mirror_path == mirror
        assert await real_git("-C", str(layout.worktree_path), "rev-parse", "HEAD") == fixture_head
        assert (layout.worktree_path / "solution.py").is_file()
    assert solution.read_text() == "independent workspace edits\n"
    assert await real_git("-C", str(existing.worktree_path), "rev-parse", "HEAD") == existing_head
    assert (
        await real_git(*git_args, "rev-parse", "refs/heads/" + existing.branch_name)
        == existing_head
    )
    assert await real_git("--git-dir", str(independent.mirror_path), "show-ref") == other_refs
    assert (independent.worktree_path / ".git").read_bytes() == other_git
    assert (independent.worktree_path / "solution.py").is_file()


async def test_missing_runtime_settles_bootstrap_operation(engine, tmp_path):
    from awf.db.repositories import OperationRepository

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="no-runtime",
    )
    provisioner = Provisioner(
        session_factory=factory, git=GitManager(tmp_path), config=ProvisionerConfig(node_id="local")
    )
    await provisioner.provision(response.workspace_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "failed"
        assert ws.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
        operation = (await OperationRepository(session).list_for_workspace(ws.id))[0]
        assert operation.status == "failed"


@pytest.mark.parametrize("remove_allowed", [True, False])
async def test_cancel_while_fixture_is_created_removes_late_fixture_without_launch(
    engine, tmp_path, monkeypatch, remove_allowed
):
    import awf.node.provider_diagnostics as diagnostics

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="cancel-provision",
    )
    real_fixture = diagnostics.fixture_worktree

    async def cancelled_fixture(*args):
        layout = await real_fixture(*args)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(response.workspace_id)
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="cancel-in-fixture")
            await session.commit()
        return layout

    monkeypatch.setattr(diagnostics, "fixture_worktree", cancelled_fixture)
    auth_root = tmp_path / "state"
    auth = auth_root / "auth" / response.workspace_id / "diagnostic" / "codex"
    auth.mkdir(parents=True)
    (auth / "auth.json").write_text("synthetic-native-credential")
    launcher = SimpleNamespace(
        launch=AsyncMock(), _auth_mount_resolver=SimpleNamespace(work_dir=auth_root)
    )
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="local"),
        stack_launcher=launcher,
    )
    if not remove_allowed:
        from awf.node.git_manager import GitOperationError

        monkeypatch.setattr(
            provisioner._git,
            "remove_worktree",
            AsyncMock(
                side_effect=GitOperationError(
                    operation="remove",
                    returncode=1,
                    stdout="",
                    stderr="fixture protected",
                    reason_code="GIT_WORKTREE_UNOWNED",
                )
            ),
        )
    await provisioner.provision(response.workspace_id)
    launcher.launch.assert_not_awaited()
    assert (tmp_path / "worktrees" / response.workspace_id).exists() is not remove_allowed
    assert auth.exists() is not remove_allowed
    if not remove_allowed:
        from sqlalchemy import select

        from awf.db.models import WorkspaceEvent

        async with factory() as session:
            event = (
                await session.scalars(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == response.workspace_id,
                        WorkspaceEvent.event_type == "workspace.terminal_runtime_release_revoked",
                    )
                )
            ).one()
            assert event.payload["cleanup_reason"] == "GIT_WORKTREE_UNOWNED"


@pytest.mark.parametrize("native_auth", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_cancel_during_stack_launch_stops_runtime_before_fixture_disposal(
    engine, tmp_path, native_auth, interrupted
):
    import asyncio

    from awf.db.repositories import OperationRepository

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="cancel-launch",
    )
    fixture = tmp_path / "worktrees" / response.workspace_id

    async def down(*_args, **kwargs):
        assert fixture.exists()
        assert auth.exists() is native_auth
        assert kwargs["remove_volumes"]
        return True

    compose = SimpleNamespace(down=AsyncMock(side_effect=down), remove_project_by_label=AsyncMock())

    async def launch(_request):
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(response.workspace_id)
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="cancel-in-launch")
            await session.commit()
        if interrupted:
            raise asyncio.CancelledError()
        return

    auth_root = tmp_path / "state"
    auth = auth_root / "auth" / response.workspace_id / "diagnostic" / "codex"
    if native_auth:
        auth.mkdir(parents=True)
        (auth / "auth.json").write_text("synthetic-native-credential")
    launcher = SimpleNamespace(
        launch=launch,
        _compose=compose,
        _auth_mount_resolver=SimpleNamespace(work_dir=auth_root) if native_auth else None,
    )
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="local"),
        stack_launcher=launcher,
    )
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            await provisioner.provision(response.workspace_id)
    else:
        await provisioner.provision(response.workspace_id)
    compose.down.assert_awaited_once()
    assert not fixture.exists()
    assert not auth.exists()
    async with factory() as session:
        ops = await OperationRepository(session).list_for_workspace(response.workspace_id)
        assert (
            next(
                op for op in ops if (op.payload or {}).get("source") == "provider_connection_test"
            ).status
            == "cancelled"
        )


@pytest.mark.parametrize("error", ["cancelled", "timeout", "already_exited", "kill_race"])
async def test_fixture_git_drains_process_on_interruption(monkeypatch, error):
    import asyncio
    from unittest.mock import Mock

    from awf.node.provider_diagnostics import _git

    failure = TimeoutError() if error == "timeout" else asyncio.CancelledError()
    process = SimpleNamespace(
        returncode=1 if error == "already_exited" else None,
        communicate=AsyncMock(side_effect=failure),
        kill=Mock(side_effect=ProcessLookupError() if error == "kill_race" else None),
        wait=AsyncMock(return_value=-9),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(type(failure)):
        await _git("status")
    process.wait.assert_awaited_once()
    assert process.kill.call_count == (0 if error == "already_exited" else 1)
    assert spawn.call_args.kwargs["env"]["HOME"] == "/nonexistent"
    assert spawn.call_args.kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


async def test_fixture_git_failure_does_not_expose_process_output():
    from awf.node.provider_diagnostics import _git

    with pytest.raises(ValueError, match="^PROVIDER_DIAGNOSTIC_FIXTURE_FAILED$"):
        await _git("not-a-git-command")
