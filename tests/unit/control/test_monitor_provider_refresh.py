"""Monitor resumes materialize promoted selections before recreating the runtime."""

from unittest.mock import AsyncMock, Mock

import pytest

from awf.common.agent_execution import resolve_execution
from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.node.compose_manager import WorkspaceComposeSpec
from awf.profiles.models import WorkspaceProfile
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    factory,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)

_IMPORTED_FIXTURES = (factory,)


@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing_launcher",
        "missing_roots",
        "missing_profile",
        "materialization",
        "bad_compose",
        "lost_claim",
    ],
)
async def test_monitor_refreshes_selected_material_before_recreate(
    factory, tmp_path, monkeypatch, failure, hosted
):
    selected = resolve_execution(
        agent="codex", provider="openrouter", model="router/model", allow_unverified=True
    )
    profile = WorkspaceProfile(name="provider")
    workspace_id = await _seed_monitoring_pr(
        factory,
        resolved_profile=profile.model_dump(mode="json"),
        task_policy={
            "agent_execution": selected.model_dump(),
            **({"pr_adoption": {"execution": {"mode": "hosted"}}} if hosted else {}),
        },
        compose_file_path=str(tmp_path / "compose.yml"),
    )
    (tmp_path / "compose.yml").write_text("services:\n  agent:\n    image: pinned\n")
    if failure == "bad_compose":
        (tmp_path / "compose.yml").write_text("malformed: [")
    if failure == "missing_profile":
        monkeypatch.setattr(monitor_handoff, "_sync_resolved_profile", AsyncMock(return_value=None))
    executor = _make_executor(FakeCommandRunner(), factory, tmp_path)
    recheck = executor._recheck_status

    async def fenced_recheck(workspace_id, **kwargs):
        if failure == "lost_claim" and kwargs.get("action") in {
            "resume_compose_locked",
            "resume_hosted_material",
        }:
            return False
        return await recheck(workspace_id, **kwargs)

    monkeypatch.setattr(executor, "_recheck_status", fenced_recheck)
    launcher = Mock(
        agent_spec=AsyncMock(
            return_value=WorkspaceComposeSpec(
                workspace_id="ws", worktree_host_path=tmp_path / "checkout"
            ),
            side_effect=RuntimeError("material unavailable")
            if failure == "materialization"
            else None,
        )
    )
    executor._stack_launcher = None if failure == "missing_launcher" else launcher
    executor._worktree_activity_git_roots = (
        None
        if failure == "missing_roots"
        else lambda **_: (tmp_path / "admin", tmp_path / "mirror.git")
    )
    monkeypatch.setattr(monitor_handoff, "lock_resumed_worktree_registration", AsyncMock())
    monkeypatch.setattr(
        monitor_handoff._runtime_setup_replay, "capture_agent_container_identity", AsyncMock()
    )

    async def restart(**kwargs):
        launcher.agent_spec.assert_awaited_once()
        request = launcher.agent_spec.call_args.args[0]
        assert request.execution == selected
        assert request.layout.worktree_path == executor._config.worktrees_root / workspace_id
        assert request.layout.mirror_path == tmp_path / "mirror.git"
        return False  # stop here: restart mechanics are covered by their existing suite

    executor._restart_compose_runtime_for_monitor_resume = AsyncMock(side_effect=restart)
    assert await executor.resume_pr_monitor_handoff(workspace_id) is None
    if failure == "lost_claim":
        launcher.agent_spec.assert_not_awaited()
        executor._restart_compose_runtime_for_monitor_resume.assert_not_awaited()
        async with factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            assert row.status == "monitoring_pr"
    elif failure:
        executor._restart_compose_runtime_for_monitor_resume.assert_not_awaited()
        async with factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            assert row.status == "failed"
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
            assert any(
                event.reason_code == "EXECUTION_CONFIGURATION_REFRESH_FAILED" for event in events
            )
    elif hosted:
        launcher.agent_spec.assert_awaited_once()
        assert launcher.agent_spec.call_args.kwargs["hosted"] is True
        executor._restart_compose_runtime_for_monitor_resume.assert_not_awaited()
    else:
        executor._restart_compose_runtime_for_monitor_resume.assert_awaited_once()


@pytest.mark.parametrize("provider", ["openai", "ollama"])
async def test_hosted_no_compose_resume_preserves_native_and_rejects_third_party(
    factory, tmp_path, provider
):
    from awf.node.compose_manager import ComposeManager
    from tests.unit.control.test_executor_monitor_recovery_parts.test_executor_monitor_recovery_hosted_seam import (
        _TEMPLATE,
        _RecordingCompose,
        _RecordingExecutor,
        _RecordingMonitor,
    )
    from tests.unit.control.test_executor_monitor_recovery_parts.test_executor_monitor_recovery_hosted_seam import (
        _make_executor as make_hosted_executor,
    )
    from tests.unit.control.test_executor_monitor_recovery_parts.test_executor_monitor_recovery_hosted_seam import (
        _seed_monitoring_pr as seed_hosted,
    )

    selected = resolve_execution(
        agent="codex", provider=provider, model="coder", allow_unverified=True
    )
    workspace_id = await seed_hosted(
        factory,
        task_policy={
            "agent_execution": selected.model_dump(),
            "pr_adoption": {"execution": {"mode": "hosted"}},
        },
        compose_project_name=None,
        compose_file_path=None,
    )
    monitor = _RecordingMonitor()
    adapters = []

    def monitor_factory(adapter, _profile, _workspace, **_kwargs):
        adapters.append(adapter)
        return monitor

    executor = make_hosted_executor(
        fake=FakeCommandRunner(),
        factory=factory,
        tmp_path=tmp_path,
        compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
        pr_monitor_factory=monitor_factory,
        agent_runtime_executor=_RecordingExecutor(),
    )
    compose = _RecordingCompose()
    executor._compose = compose
    await executor.resume_pr_monitor(workspace_id)
    assert compose.ensure_project_up_calls == []
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        if provider == "openai":
            assert len(monitor.run_calls) == 1
            assert adapters[0].execution == selected
            assert row.status == "monitoring_pr"
        else:
            assert monitor.run_calls == []
            assert adapters == []
            assert row.status == "failed"
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
            assert any(e.reason_code == "EXECUTION_CONFIGURATION_REFRESH_FAILED" for e in events)
