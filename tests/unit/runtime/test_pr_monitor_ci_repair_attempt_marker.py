"""#1216: the runner records each dispatched CI repair per head/failing-run.

``decide()`` reads the marker so a concluded CI failure outranks a new review
wave only once per head: a no-op repair cannot then starve review feedback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import OperationRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    MonitorState,
    ReportCiFailure,
    _ci_repair_attempted_state_key,
    _ci_transient_rerun_state_key,
)
from awf.runtime.pr_monitor_runner.helpers import _record_ci_repair_attempt, _with_ci_failures
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_runner_parts.test_pr_monitor_runner_part_004 import (
    _green_status,
)

_FAILURES = (
    CheckFailure(
        name="tests",
        conclusion="FAILURE",
        log_excerpt="FAILED tests/test_x.py::test_y - AssertionError",
        run_id="r1",
    ),
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
def test_record_ci_repair_attempt_sets_current_head_and_prunes_older_heads() -> None:
    old_key = _ci_repair_attempted_state_key("old", _FAILURES)
    rerun_key = _ci_transient_rerun_state_key("old", _FAILURES)
    state = MonitorState(threads_addressed_ids={old_key: "1", rerun_key: "2", "T1": "fix"})

    _record_ci_repair_attempt(state, head_sha="new", failures=_FAILURES)

    assert state.threads_addressed_ids == {
        _ci_repair_attempted_state_key("new", _FAILURES): "1",
        rerun_key: "2",
        "T1": "fix",
    }


@pytest.mark.unit
async def test_ci_repair_dispatch_records_attempt_before_running_fix(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _with_ci_failures(_green_status(), _FAILURES)
    marker = _ci_repair_attempted_state_key(status.head_sha, _FAILURES)
    seen_at_dispatch: list[bool] = []
    state = MonitorState(started_at=0.0)

    async def _run_ci_fix(**_kwargs: object) -> object:
        seen_at_dispatch.append(marker in state.threads_addressed_ids)
        raise ProviderRecoveryRetryError()

    mocker.patch.object(runner, "_run_ci_fix", _run_ci_fix)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._execute(
            action=ReportCiFailure(failures=_FAILURES),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    assert seen_at_dispatch == [True]
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert [(op.type, op.error_code) for op in operations] == [("ci_repair", "PROVIDER_OUTAGE")]
