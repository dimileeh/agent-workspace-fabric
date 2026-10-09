"""Regression tests for #1017: TLS-class git transport faults on the base refresh.

A TLS session that drops mid-``git fetch`` (``GnuTLS recv error (-110)`` and its
``gnutls_handshake()`` / ``OpenSSL SSL_read`` siblings) is the same transient class as the
DNS (#336) and 5xx faults, so it must enter the existing bounded base-fetch retry budget
instead of terminating the monitor with ``GIT_FETCH_BASE_FAILED``. Exhaustion and genuinely
deterministic fetch failures keep their current behavior and reason codes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MergeStateStatus, MonitorState, SyncBase
from awf.runtime.pr_monitor_runner.helpers import _is_transient_base_fetch_error
from awf.runtime.pr_monitor_runner.types import BaseFetchError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status

# The verbatim stderr from the two workspaces killed in issue #1017.
_GNUTLS_RECV_STDERR = (
    "git fetch base failed with exit code 128; stderr: fatal: unable to access "
    "'https://github.com/dimileeh/aira-agent.git/': GnuTLS recv error (-110): "
    "The TLS connection was non-properly terminated."
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _retrying_events(workspace: Workspace) -> list[WorkspaceEvent]:
    return [
        event
        for event in workspace.events
        if event.event_type == "monitor.git_base_fetch_transient_retrying"
    ]


async def _execute_sync_base_with_fetch_error(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    stderr: str,
    max_retries: int | None = None,
) -> tuple[str, bool | None, RecordedSleep]:
    """Drive one ``SyncBase`` action whose base refresh raises ``BaseFetchError``."""

    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    if max_retries is not None:
        object.__setattr__(runner._runner_config, "transient_base_fetch_max_retries", max_retries)

    async def _raise_base_fetch_error(**_kwargs: object) -> object:
        raise BaseFetchError(stderr)

    mocker.patch.object(runner, "_run_sync_base", _raise_base_fetch_error)

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(merge_state_status=MergeStateStatus.DIRTY),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    return workspace_id, terminal, sleep_fn


@pytest.mark.unit
def test_gnutls_recv_error_base_fetch_is_transient() -> None:
    """The exact stderr from #1017 must classify transient, not terminal."""

    assert _is_transient_base_fetch_error(BaseFetchError(_GNUTLS_RECV_STDERR))


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: unable to access 'https://github.com/org/repo.git/': "
        "gnutls_handshake() failed: Error in the pull function.",
        "fatal: unable to access 'https://github.com/org/repo.git/': "
        "OpenSSL SSL_read: Connection reset by peer, errno 104",
        "fatal: unable to access 'https://github.com/org/repo.git/': "
        "OpenSSL SSL_write: Broken pipe, errno 32",
        "error: RPC failed; curl 56 The TLS connection was non-properly terminated.",
    ],
)
def test_sibling_tls_transport_errors_are_transient(stderr: str) -> None:
    assert _is_transient_base_fetch_error(BaseFetchError(stderr))


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "git fetch base failed with exit code 128; stderr: "
        "fatal: couldn't find remote ref development",
        "git fetch origin development failed: repository not found",
        "fatal: unable to access 'https://github.com/org/repo.git/': "
        "The requested URL returned error: 403",
    ],
)
def test_non_transient_base_fetch_errors_still_fail_fast(stderr: str) -> None:
    """Widening the classifier must not swallow deterministic fetch failures.

    In particular the bare ``unable to access '<url>'`` prefix shared with the TLS
    stderr must stay non-transient on its own.
    """

    assert not _is_transient_base_fetch_error(BaseFetchError(stderr))


@pytest.mark.unit
async def test_gnutls_base_fetch_retries_and_increments_counter(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A GnuTLS drop keeps the workspace in ``monitoring_pr`` and records the retry."""

    workspace_id, terminal, sleep_fn = await _execute_sync_base_with_fetch_error(
        factory=factory,
        tmp_path=tmp_path,
        mocker=mocker,
        stderr=_GNUTLS_RECV_STDERR,
    )

    assert terminal is False
    assert sleep_fn.calls == [5]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
        assert workspace.failure_message is None
        events = _retrying_events(workspace)
        assert len(events) == 1
        assert events[0].reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "sync_base"
        assert events[0].payload["retry_number"] == 1
    assert operations[0].result["status"] == "retrying"
    assert operations[0].error_code == "GIT_BASE_FETCH_TRANSIENT_RETRY"


@pytest.mark.unit
async def test_gnutls_base_fetch_exhaustion_is_still_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """Exhausting the bounded budget keeps today's terminal reason code."""

    workspace_id, terminal, _ = await _execute_sync_base_with_fetch_error(
        factory=factory,
        tmp_path=tmp_path,
        mocker=mocker,
        stderr=_GNUTLS_RECV_STDERR,
        max_retries=0,
    )

    assert terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_message is not None
        assert "could not refresh base branch" in workspace.failure_message
        assert "GnuTLS recv error" in workspace.failure_message
        assert not _retrying_events(workspace)
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED"


@pytest.mark.unit
async def test_couldnt_find_remote_ref_fails_immediately(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A genuine non-transient fetch error still terminates with ``GIT_FETCH_BASE_FAILED``."""

    workspace_id, terminal, sleep_fn = await _execute_sync_base_with_fetch_error(
        factory=factory,
        tmp_path=tmp_path,
        mocker=mocker,
        stderr=(
            "git fetch base failed with exit code 128; stderr: "
            "fatal: couldn't find remote ref development"
        ),
    )

    assert terminal is True
    assert sleep_fn.calls == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert not _retrying_events(workspace)
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "GIT_FETCH_BASE_FAILED"
