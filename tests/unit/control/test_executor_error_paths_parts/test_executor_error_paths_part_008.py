"""Executor error-path coverage for validation coverage evidence branches."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor.helpers import _validation_run_command_records
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.pr_creator import PullRequestResult
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_identity import environment_identity_digest, resolved_profile_digest
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_003 import (
    _coverage_result,
    _make_executor,
    _RecordingPrCreator,
    _RecordingValidation,
    _seed_ready,
    _validation_command_result,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


class TestPullRequestUnexpectedErrorPart002:
    @pytest.mark.unit
    async def test_fresh_pr_workspace_without_local_coverage_records_no_coverage_command(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingPrCreatorLocal:
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
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)  # commit staged implementation output
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="validated-head\n")  # pre-validation HEAD
        # Final pre-push gates re-derive committed output: plan-only gate diffs
        # base..HEAD (name-only), then the protected-output gate diffs it again
        # (name-status). The branch has real committed work, so both pass.
        fake.queue_result(  # plan-only committed diff
            returncode=0,
            stdout="src/awf/runtime/pr_monitor_runner.py\n",
        )
        fake.queue_result(  # protected committed diff (name-status)
            returncode=0,
            stdout="M\0src/awf/runtime/pr_monitor_runner.py\0",
        )

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_RecordingPrCreatorLocal(),
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
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)  # commit staged implementation output
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="validated-head\n")  # pre-validation HEAD

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
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)  # commit staged implementation output
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="validated-head\n")  # pre-validation HEAD

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
