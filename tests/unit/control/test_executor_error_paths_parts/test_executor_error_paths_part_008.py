"""Error-path coverage for ``awf.control.executor.WorkspaceExecutor``.

Split from test_executor_error_paths_part_003.py — plan-only output and
local-coverage evidence branches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.helpers import (
    _validation_run_command_records,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.pr_creator import PullRequestResult
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from awf.runtime.validation_identity import environment_identity_digest, resolved_profile_digest
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


class _RecordingValidation:
    def __init__(
        self,
        *,
        phase_result: ValidationResult | None = None,
        coverage_result: ValidationCoverageResult | None = None,
    ) -> None:
        self._phase_result = phase_result or ValidationResult()
        self._coverage_result = coverage_result
        self.calls: list[tuple[str, ...]] = []
        self.coverage_calls: list[str | None] = []
        self.phase_kwargs: list[dict[str, Any]] = []
        self.coverage_kwargs: list[dict[str, Any]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        self.phase_kwargs.append(dict(kwargs))
        if phase_names == ("setup", "pre_agent"):
            return ValidationResult()
        return self._phase_result

    async def run_profile_coverage(self, **_kwargs: Any) -> ValidationCoverageResult | None:
        self.coverage_kwargs.append(dict(_kwargs))
        phase = _kwargs.get("phase")
        self.coverage_calls.append(phase if isinstance(phase, str) else None)
        return self._coverage_result


class _RecordingPrCreator:
    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        return PullRequestResult(
            url="https://github.com/x/y/pull/123",
            branch=branch_name,
            head_sha="b" * 40,
        )


def _validation_command_result(
    tmp_path: Path,
    *,
    returncode: int,
    reason_code: str,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "validation.stdout"
    stderr_path = tmp_path / "validation.stderr"
    stdout_path.write_text("validation stdout\n", encoding="utf-8")
    stderr_path.write_text("validation stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="validate",
        reason_code=reason_code,
    )


def _coverage_result(tmp_path: Path, *, percent: float = 99.5) -> ValidationCoverageResult:
    stdout_path = tmp_path / "coverage.stdout"
    stderr_path = tmp_path / "coverage.stderr"
    stdout_path.write_text(f"TOTAL 10 0 {percent:.1f}%\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    command_result = ValidationCommandResult(
        command="pytest --cov=awf",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="coverage",
        reason_code="COVERAGE_OK",
    )
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=99.0,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        command_result=command_result,
    )


async def _seed_ready(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    base_commit: str | None = "a" * 40,
    auto_merge: bool | None = None,
    resolved_profile: dict[str, Any] | None = None,
    requested_profile: dict[str, Any] | None = None,
    profile_ref: str | None = None,
    task_prompt: str = "p",
    task_policy: dict[str, Any] | None = None,
    owned_paths: list[str] | None = None,
    test_commands: list[str] | None = None,
    task_kind: str = "feature_branch_pr",
    initial_review_grace_period_seconds: float | None = None,
    create_task_attempt: bool = False,
    mark_canonical_attempt: bool = False,
    create_worktree: bool = True,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="err-path",
            task_prompt=task_prompt,
            agent=agent,
            test_commands=test_commands or ["pytest -q"],
            requires_database=False,
            owned_paths=owned_paths,
            task_policy=task_policy,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            task_kind=task_kind,
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=ws.task_external_id,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=ws,
            )
            if mark_canonical_attempt:
                attempt.is_canonical_for_merge = True
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = "awf/x"
        ws.remote_push_branch = "awf/x"
        ws.base_commit = base_commit
        ws.compose_project_name = "awf_x"
        if auto_merge is not None:
            ws.auto_merge = auto_merge
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _make_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    validation: _RecordingValidation | None = None,
    pr_creator: Any = None,
    pr_monitor_factory: Any = None,
    max_validation_fix_passes: int = 3,
) -> WorkspaceExecutor:
    from awf.docker.compose import ComposeManager

    compose = ComposeManager(work_dir=tmp_path / "w", template_path=_TEMPLATE)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation or _RecordingValidation(),
        pr_creator=pr_creator or _RecordingPrCreator(),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "w" / "wt",
            compose_projects_root=tmp_path / "w" / "c",
            default_models={},
            max_validation_fix_passes=max_validation_fix_passes,
        ),
        pr_monitor_factory=pr_monitor_factory,
    )


class TestPlanOnlyAndLocalCoverage:
    @pytest.mark.unit
    async def test_plan_only_output_fails_before_validation_and_pr_creation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("plan-only output must not be pushed")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=0,
            stdout=("docs/awf-plans/ws_plan.md\ndocs/awf-plans/ws_plan.conformance.json\n"),
        )
        validation = _RecordingValidation()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "only AWF plan/conformance artifact" in (ws.failure_message or "")
            assert ws.pr_url is None
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "PLAN_ONLY_OUTPUT"
                for event in events
            )
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert runs == []

        assert validation.calls == [("setup", "pre_agent")]

    @pytest.mark.unit
    async def test_plan_only_staged_conformance_after_real_commit_is_accepted(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingPrCreator:
            def __init__(self) -> None:
                self.called = False

            async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
                self.called = True
                return PullRequestResult(
                    url="https://github.com/x/y/pull/123",
                    branch=branch_name,
                    head_sha="b" * 40,
                )

        ws_id = await _seed_ready(factory)
        validation = _RecordingValidation()
        pr_creator = _RecordingPrCreator()

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=0,
            stdout=f"docs/awf-plans/{ws_id}.conformance.json\n",
        )
        fake.queue_result(
            returncode=0,
            stdout="src/awf/mcp/server.py\ntests/unit/mcp/test_mcp_operator_surfaces.py\n",
        )
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="2\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="validated-head\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=pr_creator,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert ws.pr_url == "https://github.com/x/y/pull/123"
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert not any(event.reason_code == "PLAN_ONLY_OUTPUT" for event in events)
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].workspace_head_sha == "validated-head"

        assert pr_creator.called is True
        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]

    @pytest.mark.unit
    async def test_fresh_pr_workspace_without_local_coverage_records_no_coverage_command(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingPrCreator:
            async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
                return PullRequestResult(
                    url="https://github.com/x/y/pull/123",
                    branch=branch_name,
                    head_sha="b" * 40,
                )

        class _Monitor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                self.calls.append(workspace_id)

        monitor = _Monitor()
        profile = WorkspaceProfile(
            name="scm-check-coverage",
            source="test",
            phases={"validate": ["pytest tests/unit/cli -q"]},
            validation={
                "strategy": {
                    "baseline_coverage": "skip",
                    "edit_gate": "targeted",
                },
            },
        )
        ws_id = await _seed_ready(factory, resolved_profile=profile.model_dump(mode="json"))
        validation = _RecordingValidation()

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="2\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="validated-head\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_RecordingPrCreator(),
            pr_monitor_factory=lambda *_args: monitor,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert validation.coverage_calls == []
        assert monitor.calls == [ws_id]
        assert len(runs) == 1
        coverage_commands = [cmd for cmd in runs[0].commands if cmd.get("phase") == "coverage"]
        assert coverage_commands == []

    @pytest.mark.unit
    async def test_local_coverage_is_not_marked_executed_when_phase_validation_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        profile = WorkspaceProfile(
            name="local-coverage-failure",
            source="test",
            phases={"validate": ["pytest -q"]},
            validation={
                "strategy": {"baseline_coverage": "skip", "final_gate": "coverage"},
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                },
            },
        )
        ws_id = await _seed_ready(factory, resolved_profile=profile.model_dump(mode="json"))
        validation = _RecordingValidation(
            phase_result=ValidationResult(
                commands=[
                    _validation_command_result(
                        tmp_path,
                        returncode=1,
                        reason_code="COMMAND_FAILED",
                    )
                ]
            ),
            coverage_result=_coverage_result(tmp_path),
        )

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="2\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="validated-head\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_RecordingPrCreator(),
            max_validation_fix_passes=0,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

        assert validation.coverage_calls == []
        assert len(runs) == 1
        assert runs[0].status == "failed"
        coverage_commands = [cmd for cmd in runs[0].commands if cmd.get("phase") == "coverage"]
        assert len(coverage_commands) == 1
        assert "evidence_status" not in coverage_commands[0]
        assert "evidence_reason_code" not in coverage_commands[0]

    @pytest.mark.unit
    async def test_local_coverage_reuses_fresh_evidence_before_running_command(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        profile = WorkspaceProfile(
            name="local-coverage-reuse",
            source="test",
            phases={"validate": ["pytest -q"]},
            validation={
                "strategy": {
                    "baseline_coverage": "skip",
                    "final_gate": "coverage",
                    "reuse_evidence": True,
                    "freshness_max_age_seconds": 3600,
                },
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                },
            },
        )
        ws_id = await _seed_ready(factory, resolved_profile=profile.model_dump(mode="json"))
        commands = _validation_run_command_records(
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )
        async with factory() as s:
            source_run = await ValidationRunRepository(s).start(
                workspace_id=ws_id,
                attempt_id=None,
                tier=1,
                commands=commands,
                base_commit="base",
                target_branch="development",
                target_head_sha=None,
                workspace_head_sha="validated-head",
                resolved_profile_digest=resolved_profile_digest(profile),
                environment_identity_digest=environment_identity_digest(profile),
                log_stream_refs={},
            )
            await ValidationRunRepository(s).finish(
                source_run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage=_coverage_result(tmp_path).as_metadata(),
            )
            await s.commit()
            source_run_id = source_run.id

        validation = _RecordingValidation(coverage_result=_coverage_result(tmp_path))
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="2\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="validated-head\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_RecordingPrCreator(),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

        assert validation.coverage_calls == []
        new_run = next(run for run in runs if run.id != source_run_id)
        coverage_command = next(cmd for cmd in new_run.commands if cmd.get("phase") == "coverage")
        assert coverage_command["evidence_status"] == "reused"
        assert coverage_command["evidence_reason_code"] == "VALIDATION_EVIDENCE_REUSED"
        assert coverage_command["evidence_source_run_id"] == source_run_id
