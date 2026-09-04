"""PR reuse tip-containment and monitor-factory coverage split from part_004."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import pr_open_step as _pr_open_step
from awf.control.executor.helpers import _call_pr_monitor_factory
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_004 import (
    _ForgeRecordingPrCreator,
    _LifecycleForgeClient,
    _make_executor,
    _queue_full_happy_path,
    _queue_pre_agent_symlink_baseline,
    _queue_pre_push_checks,
    _queue_validation_head,
    _seed_ready,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


class TestPullRequestUnexpectedErrorReuseTipPart025:
    @pytest.mark.unit
    async def test_reuse_push_fails_closed_when_tip_retry_lookup_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        forge = _LifecycleForgeClient(
            snapshots=[
                PullRequestSnapshot(
                    PullRequestLifecycle.open,
                    "awf/ws_original",
                    head_sha="a" * 40,
                ),
                PullRequestSnapshot(
                    PullRequestLifecycle.open,
                    "awf/ws_original",
                    head_sha="a" * 40,
                ),
                TimeoutError("forge tip-retry timed out"),
            ]
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        async def _not_descendant(**_kwargs: Any) -> bool:
            return False

        async def _no_delay(_seconds: float) -> None:
            return None

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)
        monkeypatch.setattr(_pr_open_step, "_POST_PUSH_TIP_RETRY_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(_pr_open_step.asyncio, "sleep", _no_delay)
        monkeypatch.setattr(_pr_open_step, "_live_head_descends_from_pushed", _not_descendant)

        pr_creator = _ForgeRecordingPrCreator()
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.snapshot_calls == 3
        assert len(pr_creator.calls) == 1
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.pr_url == "https://github.com/x/y/pull/55"

    @pytest.mark.unit
    async def test_reuse_push_keeps_pr_when_post_push_merge_contains_tip(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Push then concurrent merge of that same tip: monitor ShortCircuit is OK.
        pushed_tip = "b" * 40
        forge = _LifecycleForgeClient(
            snapshots=[
                PullRequestSnapshot(
                    PullRequestLifecycle.open,
                    "awf/ws_original",
                    head_sha="a" * 40,
                ),
                PullRequestSnapshot(
                    PullRequestLifecycle.merged,
                    "awf/ws_original",
                    head_sha=pushed_tip,
                ),
            ]
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        pr_creator = _ForgeRecordingPrCreator()
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.snapshot_calls == 2
        assert len(pr_creator.calls) == 1
        assert pr_creator.existing_pr_url == "https://github.com/x/y/pull/55"
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/55"

    @pytest.mark.unit
    async def test_reuse_push_opens_replacement_when_post_push_merge_excludes_tip(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Snapshot open → push → concurrent merge of the *old* tip: must replace.
        forge = _LifecycleForgeClient(
            snapshots=[
                PullRequestSnapshot(
                    PullRequestLifecycle.open,
                    "awf/ws_original",
                    head_sha="a" * 40,
                ),
                PullRequestSnapshot(
                    PullRequestLifecycle.merged,
                    "awf/ws_original",
                    head_sha="a" * 40,
                ),
            ],
            create_url="https://github.com/x/y/pull/99",
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        async def _not_descendant(**_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)
        monkeypatch.setattr(_pr_open_step, "_live_head_descends_from_pushed", _not_descendant)

        pr_creator = _ForgeRecordingPrCreator(
            new_pr_url="https://github.com/x/y/pull/99",
        )
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.snapshot_calls == 2
        assert len(pr_creator.calls) == 2
        assert pr_creator.calls[0]["existing_pr_url"] == "https://github.com/x/y/pull/55"
        assert pr_creator.calls[1]["existing_pr_url"] is None
        assert pr_creator.calls[1]["forge_client"] is forge
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/99"
            assert ws.pr_number == 99

    @pytest.mark.unit
    async def test_reuse_push_opens_replacement_when_post_push_pr_closed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        forge = _LifecycleForgeClient(
            snapshots=[
                PullRequestSnapshot(PullRequestLifecycle.open, "awf/ws_original"),
                PullRequestSnapshot(PullRequestLifecycle.closed, "awf/ws_original"),
            ],
            create_url="https://github.com/x/y/pull/99",
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        pr_creator = _ForgeRecordingPrCreator(
            new_pr_url="https://github.com/x/y/pull/99",
        )
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.snapshot_calls == 2
        assert len(pr_creator.calls) == 2
        assert pr_creator.calls[1]["existing_pr_url"] is None
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/99"

    @pytest.mark.unit
    async def test_reuse_push_fails_closed_when_post_push_lookup_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        forge = _LifecycleForgeClient(
            snapshots=[
                PullRequestSnapshot(PullRequestLifecycle.open, "awf/ws_original"),
                TimeoutError("forge lifecycle timed out after push"),
            ]
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        pr_creator = _ForgeRecordingPrCreator()
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.snapshot_calls == 2
        assert len(pr_creator.calls) == 1
        assert pr_creator.existing_pr_url == "https://github.com/x/y/pull/55"
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.pr_url == "https://github.com/x/y/pull/55"

    @pytest.mark.unit
    async def test_reuse_push_opens_replacement_when_existing_pr_merged(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the preserved PR merged after admission, abandon reuse and open a
        # replacement so the monitor does not ShortCircuitCompleted on a tip that
        # never landed in the merged PR.
        forge = _LifecycleForgeClient(lifecycle=PullRequestLifecycle.merged)

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        pr_creator = _ForgeRecordingPrCreator(
            new_pr_url="https://github.com/x/y/pull/99",
        )
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.lifecycle_calls == 1
        assert pr_creator.existing_pr_url is None
        assert pr_creator.remote_branch_name is None
        assert pr_creator.forge_client is forge
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/99"
            assert ws.pr_number == 99
            assert ws.remote_push_branch is not None

    @pytest.mark.unit
    async def test_reuse_push_fails_closed_when_lifecycle_lookup_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        forge = _LifecycleForgeClient(
            lookup_error=TimeoutError("forge lifecycle timed out"),
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        pr_creator = _ForgeRecordingPrCreator()
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/x/y/pull/55"
            ws.pr_number = 55
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.lifecycle_calls == 1
        assert pr_creator.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value

    @pytest.mark.unit
    async def test_reuse_push_fails_closed_when_pr_number_unresolvable(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # pr_url without an extractable number must fail closed like a lifecycle
        # lookup failure: keep identity and do not open a replacement PR.
        from awf.control.executor.constants import (
            _AUDIT_PR_CREATED_EVENT,
        )
        from awf.control.executor.pr_open_step import (
            _PR_STATE_LOOKUP_FAILED_REASON_CODE,
        )

        forge = _LifecycleForgeClient(lifecycle=PullRequestLifecycle.open)

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        pr_creator = _ForgeRecordingPrCreator()
        ws_id = await _seed_ready(factory)
        opaque_pr_url = "https://forge.example/x/y/merge-requests/55"
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.pr_url = opaque_pr_url
            ws.pr_number = None
            ws.remote_push_branch = "awf/ws_original"
            await s.commit()

        _queue_full_happy_path(fake)

        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert forge.lifecycle_calls == 0
        assert pr_creator.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.pr_url == opaque_pr_url
            assert ws.pr_number is None
            assert ws.remote_push_branch == "awf/ws_original"
            assert any(
                event.event_type == _AUDIT_PR_CREATED_EVENT
                and event.reason_code == _PR_STATE_LOOKUP_FAILED_REASON_CODE
                for event in ws.events
            )

    @pytest.mark.unit
    async def test_validation_target_sha_update_failure_keeps_open_pr(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        async def _fail_target_sha_update(
            *,
            validation_run_id: str,
            target_head_sha: str,
        ) -> None:
            raise RuntimeError("metadata database temporarily unavailable")

        ws_id = await _seed_ready(factory)
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_checks(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/7\n")

        executor = _make_executor(fake, factory, tmp_path)
        executor._set_validation_run_target_head_sha = _fail_target_sha_update  # type: ignore[method-assign]

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/7"
            assert ws.pr_number == 7
            assert ws.monitor_last_commit_sha == "deadbeef01"

            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].target_head_sha is None


class TestPrMonitorFactoryPath:
    @pytest.mark.unit
    def test_monitor_factory_supports_one_two_and_three_argument_forms(self) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy", auto_merge=True)

        def _one_arg(adapter: object) -> tuple[str, object]:
            return ("one", adapter)

        def _two_arg(adapter: object, profile: object) -> tuple[str, object, object]:
            return ("two", adapter, profile)

        def _three_arg(
            adapter: object,
            profile: object,
            workspace: object,
        ) -> tuple[str, object, object, object]:
            return ("three", adapter, profile, workspace)

        assert _call_pr_monitor_factory(
            _one_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("one", adapter)
        assert _call_pr_monitor_factory(
            _two_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("two", adapter, profile)
        assert _call_pr_monitor_factory(
            _three_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("three", adapter, profile, workspace)

    @pytest.mark.unit
    def test_uninspectable_factory_uses_two_argument_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy", auto_merge=True)
        calls: list[tuple[object, object]] = []

        def _monitor_factory(adapter: object, profile: object) -> object:
            calls.append((adapter, profile))
            return "monitor"

        original_signature = executor_helpers.inspect.signature

        def _signature(callable_: object) -> object:
            if callable_ is _monitor_factory:
                raise ValueError("signature unavailable")
            return original_signature(callable_)

        monkeypatch.setattr(executor_helpers.inspect, "signature", _signature)

        assert (
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )
            == "monitor"
        )
        assert calls == [(adapter, profile)]

    @pytest.mark.unit
    def test_adapter_only_factory_preserves_internal_type_error(self) -> None:
        """Adapter-only factory body TypeErrors should not be masked."""
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy")
        factory_error = TypeError("factory body broke")
        factory_calls: list[object] = []

        def _monitor_factory(adapter: object) -> object:
            factory_calls.append(adapter)
            raise factory_error

        with pytest.raises(TypeError, match="factory body broke") as exc_info:
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )

        assert exc_info.value is factory_error
        assert factory_calls == [adapter]

    @pytest.mark.unit
    def test_three_arg_factory_preserves_internal_type_error(self) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy")
        factory_error = TypeError("factory body broke after accepting workspace")
        factory_calls: list[object] = []

        def _monitor_factory(adapter: object, profile: object, workspace: object) -> object:
            factory_calls.extend([adapter, profile, workspace])
            raise factory_error

        with pytest.raises(TypeError, match="accepting workspace") as exc_info:
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )

        assert exc_info.value is factory_error
        assert factory_calls == [adapter, profile, workspace]

    @pytest.mark.unit
    async def test_factory_builds_monitor_once_and_it_runs(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Line 501: when pr_monitor_factory is provided (not a bare
        monitor), the executor calls it with the created adapter and
        drives the resulting monitor's ``run()``."""
        factory_calls: list[Any] = []
        monitor_calls: list[dict[str, Any]] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(
                    {"workspace_id": workspace_id, "compose_project": compose_project}
                )
                # Don't transition — let the executor's existing code finish.

        def _monitor_factory(adapter: Any) -> _FakeMonitor:
            factory_calls.append(adapter)
            return _FakeMonitor()

        ws_id = await _seed_ready(factory)
        # Drive the full happy path through agent→commit→validate→push→create PR.
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        _queue_pre_push_checks(fake, head="deadbeef")
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/42\n")  # gh pr create

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert len(factory_calls) == 1  # factory called with adapter exactly once
        assert len(monitor_calls) == 1  # monitor.run fired

    @pytest.mark.unit
    async def test_existing_pr_recovery_pushes_and_resumes_monitor_without_duplicate_create(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monitor_calls: list[str] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        def _monitor_factory(adapter: Any) -> _FakeMonitor:
            del adapter
            return _FakeMonitor()

        forge = _LifecycleForgeClient(
            lifecycle=PullRequestLifecycle.open,
            head_sha="deadbeef",
        )

        def _make_lifecycle_client(forge_kind: str, runner: object) -> object:
            return forge

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _make_lifecycle_client)

        ws_id = await _seed_ready(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            workspace.pr_url = "https://github.com/x/y/pull/42"
            workspace.pr_number = 42
            await session.commit()

        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        _queue_pre_push_checks(fake, head="deadbeef")
        fake.queue_result(returncode=0)  # git push

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert monitor_calls == [ws_id]
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in fake.calls)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            assert workspace.pr_url == "https://github.com/x/y/pull/42"
            assert any(event.reason_code == "PR_UPDATED" for event in workspace.events)

    @pytest.mark.unit
    async def test_executor_passes_workspace_row_to_three_arg_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        factory_workspaces: list[Any] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                return None

        def _monitor_factory(adapter: Any, profile: Any, workspace: Any) -> _FakeMonitor:
            factory_workspaces.append(workspace)
            return _FakeMonitor()

        ws_id = await _seed_ready(factory, auto_merge=False)
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        _queue_pre_push_checks(fake, head="deadbeef")
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/42\n")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert len(factory_workspaces) == 1
        assert factory_workspaces[0].id == ws_id
        assert factory_workspaces[0].auto_merge is False


class TestPrReuseIdentityClearAndBitbucketAuthPart025:
    @pytest.mark.unit
    async def test_clear_stale_pr_identity_converts_sync_feature_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Merged/closed reuse must drop adoption and become a coding feature task."""
        from awf.db.enums import TaskKind

        ws_id = await _seed_ready(
            factory,
            task_kind=TaskKind.sync_feature_pr.value,
            task_policy={
                "task_kind": TaskKind.sync_feature_pr.value,
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 55,
                    "pr_url": "https://github.com/x/y/pull/55",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                    # Same-repo adoption: no fork head to retain.
                    "head_repo_slug": "x/y",
                },
            },
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(ws_id)
            assert persisted is not None
            persisted.pr_url = "https://github.com/x/y/pull/55"
            persisted.pr_number = 55
            persisted.remote_push_branch = "feature/existing"
            await session.commit()

        class _Executor:
            _session_factory = factory

        memory_ws = SimpleNamespace(
            pr_url="https://github.com/x/y/pull/55",
            pr_number=55,
            remote_push_branch="feature/existing",
            repo_url="git@github.com:x/y.git",
            task_kind=TaskKind.sync_feature_pr.value,
            task_policy={
                "task_kind": TaskKind.sync_feature_pr.value,
                "pr_adoption": {
                    "pr_number": 55,
                    "head_ref": "feature/existing",
                    "head_repo_slug": "x/y",
                },
            },
        )
        await _pr_open_step._clear_stale_pr_identity_for_replacement(
            _Executor(),
            workspace_id=ws_id,
            ws=memory_ws,
        )

        assert memory_ws.pr_url is None
        assert memory_ws.pr_number is None
        assert memory_ws.remote_push_branch is None
        assert memory_ws.task_kind == TaskKind.feature_branch_pr.value
        assert memory_ws.task_policy == {"task_kind": TaskKind.feature_branch_pr.value}

        async with factory() as session:
            persisted = await WorkspaceRepository(session).get(ws_id)
            assert persisted is not None
            assert persisted.pr_url is None
            assert persisted.pr_number is None
            assert persisted.remote_push_branch is None
            assert persisted.task_kind == TaskKind.feature_branch_pr.value
            assert persisted.task_policy == {"task_kind": TaskKind.feature_branch_pr.value}

    @pytest.mark.unit
    async def test_clear_stale_pr_identity_retains_fork_head_repo_for_replacement(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Fork adoptions must keep head_repo_* so replacement pushes stay on the fork."""
        from awf.db.enums import TaskKind

        ws_id = await _seed_ready(
            factory,
            task_kind=TaskKind.sync_feature_pr.value,
            task_policy={
                "task_kind": TaskKind.sync_feature_pr.value,
                "pr_adoption": {
                    "repo_slug": "base-org/project",
                    "pr_number": 55,
                    "pr_url": "https://github.com/base-org/project/pull/55",
                    "head_ref": "feature/existing",
                    "head_repo_slug": "fork-owner/project",
                    "head_repo_url": "git@github.com:fork-owner/project.git",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                },
            },
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(ws_id)
            assert persisted is not None
            persisted.pr_url = "https://github.com/base-org/project/pull/55"
            persisted.pr_number = 55
            persisted.remote_push_branch = "feature/existing"
            persisted.repo_url = "git@github.com:base-org/project.git"
            await session.commit()

        class _Executor:
            _session_factory = factory

        memory_ws = SimpleNamespace(
            pr_url="https://github.com/base-org/project/pull/55",
            pr_number=55,
            remote_push_branch="feature/existing",
            repo_url="git@github.com:base-org/project.git",
            task_kind=TaskKind.sync_feature_pr.value,
            task_policy={
                "task_kind": TaskKind.sync_feature_pr.value,
                "pr_adoption": {
                    "pr_number": 55,
                    "head_ref": "feature/existing",
                    "head_repo_slug": "fork-owner/project",
                    "head_repo_url": "git@github.com:fork-owner/project.git",
                },
            },
        )
        await _pr_open_step._clear_stale_pr_identity_for_replacement(
            _Executor(),
            workspace_id=ws_id,
            ws=memory_ws,
        )

        retained = {
            "head_repo_slug": "fork-owner/project",
            "head_repo_url": "git@github.com:fork-owner/project.git",
        }
        assert memory_ws.pr_url is None
        assert memory_ws.pr_number is None
        assert memory_ws.remote_push_branch is None
        assert memory_ws.task_kind == TaskKind.feature_branch_pr.value
        assert memory_ws.task_policy == {
            "task_kind": TaskKind.feature_branch_pr.value,
            "pr_adoption": retained,
        }

        async with factory() as session:
            persisted = await WorkspaceRepository(session).get(ws_id)
            assert persisted is not None
            assert persisted.pr_url is None
            assert persisted.pr_number is None
            assert persisted.remote_push_branch is None
            assert persisted.task_kind == TaskKind.feature_branch_pr.value
            assert persisted.task_policy == {
                "task_kind": TaskKind.feature_branch_pr.value,
                "pr_adoption": retained,
            }

    @pytest.mark.unit
    async def test_clear_stale_pr_identity_skips_missing_row_but_clears_memory(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """If the row vanished mid-push, still clear the in-memory reuse identity."""
        from awf.db.enums import TaskKind

        class _Executor:
            _session_factory = factory

        memory_ws = SimpleNamespace(
            pr_url="https://github.com/x/y/pull/55",
            pr_number=55,
            remote_push_branch="feature/existing",
            repo_url="git@github.com:x/y.git",
            task_kind=TaskKind.sync_feature_pr.value,
            task_policy={
                "task_kind": TaskKind.sync_feature_pr.value,
                "pr_adoption": {"pr_number": 55},
            },
        )
        await _pr_open_step._clear_stale_pr_identity_for_replacement(
            _Executor(),
            workspace_id="ws_missing_for_clear",
            ws=memory_ws,
        )
        assert memory_ws.pr_url is None
        assert memory_ws.pr_number is None
        assert memory_ws.remote_push_branch is None
        assert memory_ws.task_kind == TaskKind.feature_branch_pr.value
        assert memory_ws.task_policy == {"task_kind": TaskKind.feature_branch_pr.value}

    @pytest.mark.unit
    async def test_sync_reuse_remote_push_branch_updates_memory_when_row_missing(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Live head-ref sync must still update the in-memory push target if the row is gone."""

        class _Executor:
            _session_factory = factory

        memory_ws = SimpleNamespace(remote_push_branch="awf/stale")
        await _pr_open_step._sync_reuse_remote_push_branch(
            _Executor(),
            workspace_id="ws_missing_for_sync",
            ws=memory_ws,
            live_head_ref="contributors/renamed-head",
        )
        assert memory_ws.remote_push_branch == "contributors/renamed-head"

    @pytest.mark.unit
    async def test_reuse_push_fails_when_bitbucket_client_construction_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reuse revalidation builds the forge client before push; auth errors fail closed."""
        from awf.common.bitbucket_client import (
            BITBUCKET_AUTH_NOT_CONFIGURED,
            BitbucketClientError,
        )
        from awf.control.executor.constants import (
            _AUDIT_GIT_PUSH_EVENT,
            _AUDIT_PR_CREATED_EVENT,
            _PR_CREATE_FAILED_REASON_CODE,
        )

        def _raise_make_forge_client(forge: str, runner: object) -> object:
            raise BitbucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_API_TOKEN is required.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )

        monkeypatch.setattr(_pr_open_step, "make_forge_client", _raise_make_forge_client)

        pr_creator = _ForgeRecordingPrCreator()
        ws_id = await _seed_ready(factory)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.repo_url = "git@bitbucket.org:workspace/repo.git"
            ws.pr_url = "https://bitbucket.org/workspace/repo/pull-requests/55"
            ws.pr_number = 55
            ws.remote_push_branch = "feature/existing"
            await session.commit()

        _queue_full_happy_path(fake)
        executor = _make_executor(fake, factory, tmp_path, pr_creator=pr_creator)
        await executor.execute(ws_id)

        assert pr_creator.calls == []
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.pr_url == "https://bitbucket.org/workspace/repo/pull-requests/55"
            assert ws.events[-1].reason_code == BITBUCKET_AUTH_NOT_CONFIGURED
            pr_create_events = [
                event for event in ws.events if event.event_type == _AUDIT_PR_CREATED_EVENT
            ]
            assert len(pr_create_events) == 1
            assert pr_create_events[0].reason_code == _PR_CREATE_FAILED_REASON_CODE
            assert pr_create_events[0].payload["outcome"] == "failed"
            assert not [event for event in ws.events if event.event_type == _AUDIT_GIT_PUSH_EVENT]
