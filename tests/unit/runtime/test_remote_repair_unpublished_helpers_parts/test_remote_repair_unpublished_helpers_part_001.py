"""Unit tests for remote unpublished-repair helper functions (part 001)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished
from tests.unit.runtime.test_remote_repair_unpublished_helpers_parts._helpers import (
    _allow_repair_prerequisites,
    _allow_repair_provenance,
    _operation,
    _repair_runner,
    _repair_worktree,
)


@pytest.mark.unit
def test_verified_awf_comment_repair_worktree_rejects_mismatched_layout(tmp_path: Path) -> None:
    workspace_id = "ws_layout"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: missing\n", encoding="utf-8")
    runner = SimpleNamespace(_worktrees_root=tmp_path)
    assert (
        remote_repair_unpublished._verified_awf_comment_repair_worktree(
            runner=runner,
            workspace_id=workspace_id,
            worktree_path=tmp_path / "other",
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected_source", "expected_action"),
    [
        ({}, None, None),
        ({"source_head_sha": "  abc  ", "action": "  repair  "}, "abc", "repair"),
        ({"source_head_sha": " ", "action": 3}, None, None),
    ],
)
def test_operation_payload_helpers_normalize_strings(
    payload: object,
    expected_source: str | None,
    expected_action: str | None,
) -> None:
    operation = _operation(payload=payload)
    assert (
        remote_repair_unpublished._operation_payload_source_head_sha(operation) == expected_source
    )
    assert remote_repair_unpublished._operation_payload_action(operation) == expected_action


@pytest.mark.unit
@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (_operation(status=OperationStatus.failed.value, result={"pushed": True}), False),
        (_operation(status=OperationStatus.succeeded.value, result="invalid"), False),
        (
            _operation(
                status=OperationStatus.succeeded.value,
                result={"pushed": False, "outcome": "complete"},
            ),
            False,
        ),
    ],
)
def test_operation_result_was_pushed_rejects_non_push_results(
    operation: Operation,
    expected: bool,
) -> None:
    assert remote_repair_unpublished._operation_result_was_pushed(operation) is expected


@pytest.mark.unit
def test_operation_mapping_head_sha_skips_blank_values() -> None:
    assert remote_repair_unpublished._operation_mapping_head_sha(None, ("head",)) is None
    assert (
        remote_repair_unpublished._operation_mapping_head_sha(
            {"head": " ", "fallback": "  abc  "},
            ("head", "fallback"),
        )
        == "abc"
    )


@pytest.mark.unit
def test_operation_terminal_head_checks_all_recorded_locations() -> None:
    direct = _operation(result={"terminal_head_sha": " a "})
    evidence = _operation(
        result={"agent_service_recovery": "invalid", "failure_evidence": {"head_sha": " b "}}
    )
    payload = _operation(payload={"local_terminal_head_sha": " c "}, result="invalid")
    missing = _operation(payload="invalid", result="invalid")
    recovery = _operation(result={"agent_service_recovery": {"terminal_head_sha": " d "}})
    evidence_missing = _operation(
        payload={"terminal_head_sha": " e "},
        result={"failure_evidence": {"head_sha": " "}},
    )
    empty_recovery = _operation(
        result={"agent_service_recovery": {}, "failure_evidence": {"head_sha": " f "}}
    )

    assert remote_repair_unpublished._operation_recorded_local_terminal_head(direct) == "a"
    assert remote_repair_unpublished._operation_recorded_local_terminal_head(evidence) == "b"
    assert remote_repair_unpublished._operation_recorded_local_terminal_head(payload) == "c"
    assert remote_repair_unpublished._operation_recorded_local_terminal_head(missing) is None
    assert remote_repair_unpublished._operation_recorded_local_terminal_head(recovery) == "d"
    assert (
        remote_repair_unpublished._operation_recorded_local_terminal_head(evidence_missing) == "e"
    )
    assert remote_repair_unpublished._operation_recorded_local_terminal_head(empty_recovery) == "f"


@pytest.mark.unit
async def test_comment_provenance_filters_excluded_hint_and_inactive_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = "a" * 40
    terminal = "b" * 40
    operations = [
        _operation(
            payload={"source_head_sha": remote}, result={"local_terminal_head_sha": terminal}
        ),
        _operation(
            payload={"source_head_sha": remote, "action": "operator_hint_repair"},
            result={"local_terminal_head_sha": terminal},
        ),
        _operation(
            payload={"source_head_sha": remote},
            result={},
            status=OperationStatus.succeeded.value,
        ),
        _operation(
            payload={"source_head_sha": remote}, result={"local_terminal_head_sha": terminal}
        ),
    ]
    operations[0].id = "excluded"
    operations[1].id = "hint"
    operations[2].id = "inactive"
    operations[3].id = "owner"

    class _SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def list_for_workspace(self, *_args: object, **_kwargs: object) -> list[Operation]:
            return operations

    monkeypatch.setattr(remote_repair_unpublished, "OperationRepository", _Repository)
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=_SessionContext))
    assert await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
        runner,
        workspace_id="ws",
        remote_pr_head=remote,
        discarded_local_head=terminal,
        exclude_operation_id="excluded",
    )


@pytest.mark.unit
async def test_non_comment_provenance_skips_inactive_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = "a" * 40
    terminal = "b" * 40
    operations = [
        _operation(
            payload={"source_head_sha": remote},
            result={},
            status=OperationStatus.succeeded.value,
            operation_type=OperationType.ci_repair.value,
        ),
        _operation(
            payload={"source_head_sha": remote},
            result={"local_terminal_head_sha": terminal},
            operation_type=OperationType.ci_repair.value,
        ),
    ]

    class _SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def list_for_workspace(self, *_args: object, **_kwargs: object) -> list[Operation]:
            return operations

    monkeypatch.setattr(remote_repair_unpublished, "OperationRepository", _Repository)
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=_SessionContext))
    assert await remote_repair_unpublished._unpublished_non_comment_repair_has_operation_provenance(
        runner,
        workspace_id="ws",
        remote_pr_head=remote,
        discarded_local_head=terminal,
    )


@pytest.mark.unit
def test_operation_commit_ownership_rejects_mismatched_provenance() -> None:
    remote = "a" * 40
    terminal = "b" * 40
    operation = _operation(
        payload={"source_head_sha": remote},
        result={"local_terminal_head_sha": terminal},
    )

    assert (
        remote_repair_unpublished._operation_owns_discarded_commits(
            operation,
            remote_pr_head="c" * 40,
            discarded_local_head=terminal,
        )
        is False
    )
    operation.result = {"local_terminal_head_sha": remote}
    assert (
        remote_repair_unpublished._operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote,
            discarded_local_head=terminal,
        )
        is False
    )
    operation.result = {"local_terminal_head_sha": "d" * 40}
    assert (
        remote_repair_unpublished._operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote,
            discarded_local_head=terminal,
        )
        is False
    )


@pytest.mark.unit
def test_active_unpublished_operation_accepts_unpushed_running_operation() -> None:
    assert remote_repair_unpublished._is_active_unpublished_repair_operation(
        _operation(result={"pushed": False})
    )


@pytest.mark.unit
def test_verified_awf_comment_repair_worktree_accepts_reciprocal_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_layout"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / workspace_id
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    runner = SimpleNamespace(_worktrees_root=tmp_path)
    monkeypatch.setattr(
        remote_repair_unpublished,
        "linked_worktree_git_dir",
        lambda _path: linked_git_dir,
    )
    monkeypatch.setattr(remote_repair_unpublished, "mirror_path_for_worktree", lambda _path: mirror)
    monkeypatch.setattr(
        remote_repair_unpublished,
        "linked_worktree_path_from_git_dir",
        lambda _path: worktree,
    )

    assert remote_repair_unpublished._verified_awf_comment_repair_worktree(
        runner=runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
    )


@pytest.mark.unit
@pytest.mark.parametrize("layout_failure", ["missing_link", "missing_mirror", "bad_metadata"])
def test_verified_awf_comment_repair_worktree_rejects_invalid_git_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_failure: str,
) -> None:
    workspace_id = "ws_layout"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / workspace_id
    mirror = tmp_path / "mirror.git"
    if layout_failure != "missing_mirror":
        mirror.mkdir()
    runner = SimpleNamespace(_worktrees_root=tmp_path)
    monkeypatch.setattr(
        remote_repair_unpublished,
        "linked_worktree_git_dir",
        lambda _path: None if layout_failure == "missing_link" else linked_git_dir,
    )
    monkeypatch.setattr(remote_repair_unpublished, "mirror_path_for_worktree", lambda _path: mirror)

    def _registered_path(_path: Path) -> Path:
        if layout_failure == "bad_metadata":
            raise GitOperationError(
                operation="resolve worktree",
                returncode=1,
                stdout="",
                stderr="invalid",
            )
        return worktree

    monkeypatch.setattr(
        remote_repair_unpublished,
        "linked_worktree_path_from_git_dir",
        _registered_path,
    )
    assert (
        remote_repair_unpublished._verified_awf_comment_repair_worktree(
            runner=runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
        )
        is False
    )


@pytest.mark.unit
def test_verified_awf_comment_repair_worktree_handles_resolution_failure() -> None:
    class _BrokenRoot:
        def __truediv__(self, _child: str) -> _BrokenRoot:
            return self

        def resolve(self) -> Path:
            raise OSError("unresolvable")

    runner = SimpleNamespace(_worktrees_root=_BrokenRoot())
    assert (
        remote_repair_unpublished._verified_awf_comment_repair_worktree(
            runner=runner,
            workspace_id="ws_layout",
            worktree_path=Path("/tmp/ws_layout"),
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("returncode", "stdout", "pinned", "expected"),
    [
        (1, "", "aaa", (False, None)),
        (0, "bbb\n", "aaa", (False, "bbb")),
        (0, "AAA\n", "aaa", (True, "AAA")),
    ],
)
async def test_live_head_matches_pinned_recovery_head_outcomes(
    returncode: int,
    stdout: str,
    pinned: str,
    expected: tuple[bool, str | None],
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=returncode, stdout=stdout)
    assert (
        await remote_repair_unpublished._live_head_matches_pinned_recovery_head(
            cmd,
            worktree_path=Path("/tmp/repo"),
            pinned_head=pinned,
            git_env={},
        )
        == expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("head_failure", (False, None, False, False)),
        ("head_mismatch", (False, "bbb", False, False)),
        ("status_failure", (False, "aaa", False, False)),
        ("dirty", (False, "aaa", True, False)),
        ("reset_failure", (True, "aaa", False, False)),
        ("success", (True, "aaa", False, True)),
    ],
)
async def test_recovery_hard_reset_outcomes(
    tmp_path: Path,
    case: str,
    expected: tuple[bool, str | None, bool, bool],
) -> None:
    cmd = FakeCommandRunner()
    if case == "head_failure":
        cmd.queue_result(returncode=1, stdout="")
    elif case == "head_mismatch":
        cmd.queue_result(returncode=0, stdout="bbb\n")
    else:
        cmd.queue_result(returncode=0, stdout="aaa\n")
        if case == "status_failure":
            cmd.queue_result(returncode=1, stderr="status failed")
        elif case == "dirty":
            cmd.queue_result(returncode=0, stdout=" M src/a.py\0")
        else:
            cmd.queue_result(returncode=0, stdout="")
            cmd.queue_result(
                returncode=1 if case == "reset_failure" else 0,
                stderr="reset failed" if case == "reset_failure" else "",
            )

    result = await remote_repair_unpublished._run_recovery_hard_reset_under_writer_lock(
        cmd,
        worktree_path=tmp_path / f"ws_{case}",
        pinned_head="aaa",
        reset_target="remote",
        git_env={"GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert (result.ready, result.live_head, result.worktree_dirty, result.reset_ok) == expected
    if case == "reset_failure":
        assert result.reset_stderr == "reset failed"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("head_mismatch", (False, "bbb", False)),
        ("status_failure", (False, "aaa", True)),
        ("dirty", (False, "aaa", True)),
        ("clean", (True, "aaa", False)),
    ],
)
async def test_live_worktree_ready_for_recovery_reset_outcomes(
    case: str,
    expected: tuple[bool, str | None, bool],
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="bbb\n" if case == "head_mismatch" else "aaa\n")
    if case != "head_mismatch":
        cmd.queue_result(
            returncode=1 if case == "status_failure" else 0,
            stdout=" M src/a.py\0" if case == "dirty" else "",
        )
    assert (
        await remote_repair_unpublished._live_worktree_ready_for_recovery_reset(
            cmd,
            worktree_path=Path("/tmp/repo"),
            pinned_head="aaa",
            git_env={},
        )
        == expected
    )


@pytest.mark.unit
async def test_recovery_hard_reset_reports_writer_lock_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.asynccontextmanager
    async def _lock_failure(_path: Path):  # type: ignore[no-untyped-def]
        raise OSError("lock denied")
        yield

    monkeypatch.setattr(
        remote_repair_unpublished,
        "hold_exclusive_worktree_writer_lock",
        _lock_failure,
    )
    result = await remote_repair_unpublished._run_recovery_hard_reset_under_writer_lock(
        FakeCommandRunner(),
        worktree_path=tmp_path / "ws_lock_error",
        pinned_head="aaa",
        reset_target="bbb",
        git_env={},
    )
    assert result.writer_lock_failed is True
    assert result.reset_stderr == "lock denied"


@pytest.mark.unit
async def test_abandon_unpublished_repair_handles_worktree_resolution_failure(
    tmp_path: Path,
) -> None:
    worktree = _repair_worktree(tmp_path)

    class _BrokenRoot:
        def __truediv__(self, _child: str) -> _BrokenRoot:
            return self

        def resolve(self) -> Path:
            raise OSError("unresolvable")

    runner = _repair_runner(tmp_path, FakeCommandRunner())
    runner._worktrees_root = _BrokenRoot()
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert restored == "b" * 40
    assert result is not None
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_abandon_unpublished_repair_short_circuits_matching_heads(tmp_path: Path) -> None:
    worktree = _repair_worktree(tmp_path)
    head = "a" * 40
    cmd = FakeCommandRunner()
    # Equality short-circuit must re-read live HEAD under the writer lock.
    cmd.queue_result(returncode=0, stdout=f"{head}\n")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=head,
        local_head=head.upper(),
        state=MonitorState(),
    )
    assert restored == head
    assert result is None
    assert any("rev-parse" in call.args and "HEAD" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_abandon_unpublished_repair_rejects_layout_and_ownership_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _repair_worktree(tmp_path)
    runner = _repair_runner(tmp_path, FakeCommandRunner())
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: False,
    )
    _, layout_result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert layout_result is not None
    assert layout_result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
    )

    async def _ownership_failed(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        remote_repair_unpublished,
        "repair_agent_runtime_ownership",
        _ownership_failed,
    )
    _, ownership_result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert ownership_result is not None
    assert ownership_result.reason_code == "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"


@pytest.mark.unit
async def test_abandon_unpublished_repair_rejects_stale_snapshot_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    fetched = "b" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{fetched}\n")
    cmd.queue_result(returncode=1, stderr="published descendant check failed")
    cmd.queue_result(returncode=1, stderr="stale snapshot mismatch")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head=fetched,
        state=MonitorState(),
    )
    assert restored == fetched
    assert result is not None
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_abandon_unpublished_repair_accepts_already_published_local_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    expected = "a" * 40
    fetched = "b" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{fetched}\n")
    cmd.queue_result(returncode=0)

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=expected,
        local_head=fetched,
        state=MonitorState(),
    )
    assert restored == fetched
    assert result is None


@pytest.mark.unit
async def test_abandon_unpublished_repair_rejects_local_off_stale_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    expected = "a" * 40
    fetched = "b" * 40
    local = "c" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{fetched}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=1)

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=expected,
        local_head=local,
        state=MonitorState(),
    )
    assert restored == local
    assert result is not None
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        "delta_failure",
        "delta_parse_failure",
        "reset_failure",
        "verification_failure",
        "success",
        "success_without_event_sink",
    ],
)
async def test_abandon_unpublished_repair_terminal_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = "a" * 40
    local = "b" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    if case == "delta_failure":
        cmd.queue_result(returncode=1, stderr="diff failed")
    elif case == "delta_parse_failure":
        cmd.queue_result(returncode=0, stdout="R100\0src/old.py\0")
    else:
        cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")

    reset_ok = case not in {"reset_failure"}

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=reset_ok,
            reset_stderr="reset failed" if not reset_ok else "",
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    appended: list[object] = []
    runner = _repair_runner(tmp_path, cmd)
    if case in {"verification_failure", "success", "success_without_event_sink"}:
        cmd.queue_result(
            returncode=1 if case == "verification_failure" else 0,
            stdout="" if case == "verification_failure" else f"{remote}\n",
        )
        cmd.queue_result(returncode=0, stdout="")

        async def _append(**kwargs: object) -> None:
            appended.extend(list(kwargs["events"]))  # type: ignore[arg-type]

        if case != "success_without_event_sink":
            runner._append_workspace_events = _append

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=MonitorState(),
    )
    if case in {"success", "success_without_event_sink"}:
        assert restored == remote
        assert result is None
        assert len(appended) == (1 if case == "success" else 0)
    else:
        assert restored == local
        assert result is not None
        assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize("case", ["reset_failure", "verification_failure"])
async def test_abandon_lagging_repair_handles_fast_forward_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = "b" * 40
    local = "a" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1, stderr="not ahead")
    cmd.queue_result(returncode=0)
    if case == "verification_failure":
        cmd.queue_result(returncode=1, stderr="verify failed")
        cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=case != "reset_failure",
            reset_stderr="reset failed" if case == "reset_failure" else "",
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=MonitorState(),
    )
    assert restored == local
    assert result is not None
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"


@pytest.mark.unit
async def test_abandon_lagging_repair_fast_forwards_and_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = "b" * 40
    local = "a" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=MonitorState(),
    )
    assert restored == remote
    assert result is None
