"""Tests for non-check async reviewer settle policy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository
from awf.db.session import make_session_factory
from awf.runtime import pr_monitor_runner as runner_mod
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import (
    _non_check_reviewer_settle_decision,
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_skip_visible_key,
    _non_check_reviewer_settle_started_key,
    _normalize_non_check_reviewer_logins,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
    thread_node,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _ready_status(
    *,
    pr_number: int = 93,
    head_sha: str = "head-a",
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=pr_number,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


@pytest.mark.unit
def test_monitor_config_defaults_include_narrow_greptile_policy() -> None:
    cfg = MonitorConfig()

    assert cfg.non_check_reviewer_settle_seconds == 900
    assert cfg.non_check_reviewer_logins == (
        "greptile-apps",
        "chatgpt-codex-connector",
    )
    assert "coderabbitai" not in cfg.non_check_reviewer_logins
    assert "[bot]" not in cfg.non_check_reviewer_logins


@pytest.mark.unit
def test_non_check_reviewer_login_normalization_is_conservative() -> None:
    assert _normalize_non_check_reviewer_logins(
        [" Greptile-Apps ", "greptile-apps[bot]", "GREPTILE-APPS", "  "]
    ) == ("greptile-apps",)
    assert _normalize_non_check_reviewer_logins(["coderabbitai"]) == ("coderabbitai",)


@pytest.mark.unit
def test_non_check_reviewer_wait_starts_for_green_pr_without_visible_reviewer_check() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        poll_interval_seconds=60,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(checks=(CheckTiming(name="ci/build", conclusion="SUCCESS"),)),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "started"
    assert decision.wait_seconds == 60
    assert decision.missing_reviewers == ("greptile-apps",)
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_started_key(pr_number=93, head_sha="head-a")
        ]
        == "1000.000000"
    )
    assert (
        _non_check_reviewer_settle_done_key(pr_number=93, head_sha="head-a")
        not in state.threads_addressed_ids
    )


@pytest.mark.unit
def test_zero_poll_interval_still_waits_for_non_check_reviewer_settle() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        poll_interval_seconds=0,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(checks=(CheckTiming(name="ci/build", conclusion="SUCCESS"),)),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "started"
    assert decision.wait_seconds == 180
    assert (
        _non_check_reviewer_settle_done_key(pr_number=93, head_sha="head-a")
        not in state.threads_addressed_ids
    )


@pytest.mark.unit
def test_non_check_reviewer_wait_is_disabled_without_state_mutation() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "disabled"
    assert decision.wait_seconds == 0
    assert state.threads_addressed_ids == {}


@pytest.mark.unit
def test_non_check_reviewer_wait_is_skipped_for_manual_merge_mode() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=False,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "not_auto_merge"
    assert decision.wait_seconds == 0
    assert state.threads_addressed_ids == {}


@pytest.mark.unit
def test_visible_greptile_check_skips_extra_wait() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    status = _ready_status(checks=(CheckTiming(name="Greptile", conclusion="SUCCESS"),))

    decision = _non_check_reviewer_settle_decision(
        status,
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "visible_check"
    assert decision.wait_seconds == 0
    assert decision.visible_reviewers == ("greptile-apps",)
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_skip_visible_key(pr_number=93, head_sha="head-a")
        ]
        == "visible_check"
    )


@pytest.mark.unit
def test_no_configured_non_check_reviewers_is_noop() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=(),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "no_configured_reviewers"
    assert decision.wait_seconds == 0
    assert state.threads_addressed_ids == {}


@pytest.mark.unit
def test_visible_check_skip_is_deduped_per_head() -> None:
    state = MonitorState(
        threads_addressed_ids={
            _non_check_reviewer_settle_skip_visible_key(
                pr_number=93, head_sha="head-a"
            ): "visible_check"
        }
    )
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(checks=(CheckTiming(name="Greptile", conclusion="SUCCESS"),)),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "visible_check"
    assert decision.state_changed is False
    assert state.threads_addressed_ids == {
        _non_check_reviewer_settle_skip_visible_key(
            pr_number=93, head_sha="head-a"
        ): "visible_check"
    }


@pytest.mark.unit
def test_pr_166_regression_visible_greptile_check_still_waits_for_codex_review() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        non_check_reviewer_settle_seconds=900,
        non_check_reviewer_logins=("greptile-apps", "chatgpt-codex-connector"),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(checks=(CheckTiming(name="Greptile Review", conclusion="SUCCESS"),)),
        state,
        cfg,
        pr_number=166,
        now=1000.0,
    )

    assert decision.action == "started"
    assert decision.wait_seconds == 60
    assert decision.visible_reviewers == ("greptile-apps",)
    assert decision.missing_reviewers == ("chatgpt-codex-connector",)
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_started_key(pr_number=166, head_sha="head-a")
        ]
        == "1000.000000"
    )


@pytest.mark.unit
def test_done_key_skips_wait_for_same_head() -> None:
    state = MonitorState(
        threads_addressed_ids={
            _non_check_reviewer_settle_done_key(pr_number=93, head_sha="head-a"): "elapsed"
        }
    )
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "already_elapsed"
    assert decision.wait_seconds == 0


@pytest.mark.unit
def test_invalid_started_marker_restarts_wait_for_current_head() -> None:
    state = MonitorState(
        threads_addressed_ids={
            _non_check_reviewer_settle_started_key(pr_number=93, head_sha="head-a"): "not-a-float"
        }
    )
    cfg = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(),
        state,
        cfg,
        pr_number=93,
        now=2000.0,
    )

    assert decision.action == "started"
    assert decision.started_at == 2000.0
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_started_key(pr_number=93, head_sha="head-a")
        ]
        == "2000.000000"
    )


@pytest.mark.unit
def test_greptile_visible_check_matching_accepts_provider_context_suffix() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps", "custom-reviewer"),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(
            checks=(
                CheckTiming(name="ci/greptile", conclusion="SUCCESS"),
                CheckTiming(name="custom-reviewer / review", conclusion="SUCCESS"),
            )
        ),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "visible_check"
    assert decision.visible_reviewers == ("greptile-apps", "custom-reviewer")


@pytest.mark.unit
def test_visible_check_matching_accepts_provider_identity_metadata() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        auto_merge=True,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps", "custom-reviewer", "status-bot"),
    )

    decision = _non_check_reviewer_settle_decision(
        _ready_status(
            checks=(
                CheckTiming(
                    name="ci/review",
                    app_slug="greptile-apps",
                    conclusion="SUCCESS",
                ),
                CheckTiming(
                    name="Review",
                    app_name="Custom Reviewer",
                    conclusion="SUCCESS",
                ),
                CheckTiming(
                    name="commit-status",
                    creator_login="status-bot[bot]",
                    conclusion="SUCCESS",
                ),
            )
        ),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )

    assert decision.action == "visible_check"
    assert decision.visible_reviewers == ("greptile-apps", "custom-reviewer", "status-bot")


@pytest.mark.unit
def test_wait_is_per_head_sha_and_restarts_after_new_head() -> None:
    state = MonitorState()
    cfg = MonitorConfig(
        poll_interval_seconds=60,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    first = _non_check_reviewer_settle_decision(
        _ready_status(head_sha="head-a"),
        state,
        cfg,
        pr_number=93,
        now=1000.0,
    )
    elapsed = _non_check_reviewer_settle_decision(
        _ready_status(head_sha="head-a"),
        state,
        cfg,
        pr_number=93,
        now=1181.0,
    )
    restarted = _non_check_reviewer_settle_decision(
        _ready_status(head_sha="head-b"),
        state,
        cfg,
        pr_number=93,
        now=1182.0,
    )

    assert first.action == "started"
    assert elapsed.action == "elapsed"
    assert elapsed.wait_seconds == 0
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_done_key(pr_number=93, head_sha="head-a")
        ]
        == "elapsed"
    )
    assert restarted.action == "started"
    assert restarted.wait_seconds == 60
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_started_key(pr_number=93, head_sha="head-b")
        ]
        == "1182.000000"
    )


@pytest.mark.unit
async def test_execute_merge_blocks_pr_93_regression_until_non_check_reviewer_settles(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, pr_number=93, head_sha="head-a")
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=ws_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=93,
        status=_ready_status(
            checks=(
                CheckTiming(name="ci/build", status="COMPLETED", conclusion="SUCCESS"),
                CheckTiming(name="lint", status="COMPLETED", conclusion="SUCCESS"),
            )
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert (
        _non_check_reviewer_settle_started_key(pr_number=93, head_sha="head-a")
        in state.threads_addressed_ids
    )
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    assert [(op.type, op.status, op.payload["action"]) for op in operations] == [
        (
            OperationType.monitor_state.value,
            OperationStatus.succeeded.value,
            "reviewer_settle_wait",
        )
    ]
    assert operations[0].payload["reason_code"] == "NON_CHECK_REVIEWER_SETTLE"
    assert operations[0].payload["missing_reviewers"] == ["greptile-apps"]


@pytest.mark.unit
async def test_execute_merge_skips_extra_wait_when_greptile_has_visible_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, pr_number=94, head_sha="head-a")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="merge-sha\n")
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=ws_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=94,
        status=_ready_status(
            pr_number=94,
            checks=(CheckTiming(name="Greptile", status="COMPLETED", conclusion="SUCCESS"),),
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert sleep_fn.calls == []
    assert any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_skip_visible_key(pr_number=94, head_sha="head-a")
        ]
        == "visible_check"
    )


@pytest.mark.unit
async def test_elapsed_non_check_wait_proceeds_to_existing_merge_path(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, pr_number=95, head_sha="head-a")
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: 1181.0)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="merge-sha\n")
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    state = MonitorState(
        threads_addressed_ids={
            _non_check_reviewer_settle_started_key(pr_number=95, head_sha="head-a"): ("1000.000000")
        }
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=ws_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=95,
        status=_ready_status(pr_number=95),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert sleep_fn.calls == []
    assert any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_done_key(pr_number=95, head_sha="head-a")
        ]
        == "elapsed"
    )


@pytest.mark.unit
async def test_comments_arriving_during_non_check_wait_route_to_address_comments(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, pr_number=96, head_sha="head-a")

    class _ClockSleep:
        def __init__(self) -> None:
            self.now = 1000.0
            self.calls: list[float] = []

        async def __call__(self, seconds: float) -> None:
            self.calls.append(seconds)
            self.now += seconds

    clock = _ClockSleep()
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: clock.now)

    cmd = FakeCommandRunner()
    thread = thread_node(tid="T_late", author="greptile-apps")
    # Iteration 1: otherwise merge-ready, but Greptile has no visible check.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(head_sha="head-a"))
    # Iteration 2: a Greptile comment arrives during the wait.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(head_sha="head-a", threads=[thread]))
    # Fix-cycle settle fetch sees the burst quiet down.
    cmd.queue_result(returncode=0, stdout=pr_payload(head_sha="head-b"))
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="head-b\n")  # rev-parse
    cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve thread
    # New head restarts the non-check reviewer wait. Once that quiet period
    # elapses, the stricter merge gate must still require fresh validation for
    # the pushed fix head before merge.
    for _ in range(4):
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(head_sha="head-b"))
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="merge-sha\n")

    adapter = FakeAdapter()
    adapter.queue(stdout="fixed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=clock,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
        max_outer_iterations=8,
    )

    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    action_names = [entry["action"] for entry in captured if entry.get("event") == "monitor.action"]
    assert action_names[:2] == ["Merge", "AddressComments"]
    assert adapter.calls
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert clock.calls[:2] == [60, 30]
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    assert recovery_operations
    assert recovery_operations[-1].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
    assert recovery_operations[-1].payload["source_head_sha"] == "head-b"
    assert any(
        entry.get("event") == "monitor.non_check_reviewer_settle_started" for entry in captured
    )
