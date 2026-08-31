"""Unit tests for remote unpublished-repair helper functions."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished


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


def _operation(
    *,
    payload: object = None,
    result: object = None,
    status: str = OperationStatus.running.value,
    operation_type: str = OperationType.comment_repair.value,
) -> Operation:
    return Operation(
        id="op",
        workspace_id="ws",
        type=operation_type,
        status=status,
        payload=payload,
        result=result,
    )


def _repair_runner(tmp_path: Path, command_runner: object) -> SimpleNamespace:
    async def _fetch(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=command_runner),
        _remote_branch_fetch_once=_fetch,
    )


def _repair_worktree(tmp_path: Path, workspace_id: str = "ws_repair") -> Path:
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
    return worktree


def _allow_repair_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
    )

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair_unpublished, "repair_agent_runtime_ownership", _ownership_ok)


def _allow_repair_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _comment_provenance(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _no_conflicting_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _comment_provenance,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _no_conflicting_provenance,
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


# Production SHAs: published PR head 5c… vs orphaned hosted terminal e7….
_PUBLISHED_PR_HEAD = "5c" * 20
_ORPHANED_HOSTED_TERMINAL = "e7" * 20


def _hosted_orphan_monitor_state() -> MonitorState:
    state = MonitorState(last_push_sha=_ORPHANED_HOSTED_TERMINAL)
    state.hosted_terminal_head_advanced = True
    return state


@pytest.mark.unit
async def test_matching_heads_reconciles_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    """Equality short-circuit must still clear orphaned hosted push-tracking.

    After a successful reset that crashes before ``_persist_state``, or when
    upgrading an already-affected workspace, local HEAD already equals the
    expected remote tip while ``last_push_sha`` still advertises the abandoned
    unpublished SHA. Reconcile on that verified-equality path so hosted
    identity cannot keep failing closed.
    """
    from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace

    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=remote,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False
    workspace = SimpleNamespace(
        repo_url="https://github.com/example/repo",
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        branch_base="main",
        remote_push_branch="awf/ws_repair",
        owned_paths=[],
        task_policy={},
        monitor_last_commit_sha=_ORPHANED_HOSTED_TERMINAL,
    )
    assert hosted_pr_identity_for_workspace(workspace, state=state)["expected_head_sha"] == remote


@pytest.mark.unit
async def test_matching_heads_race_preserves_orphaned_hosted_push_tracking(
    tmp_path: Path,
) -> None:
    """Stale local==expected must not reconcile when live HEAD advanced under race.

    Reset paths recheck HEAD under the writer lock before mutating state; the
    equality short-circuit must do the same so a concurrent writer cannot leave
    push-tracking rewound while the checkout already moved past the accepted tip.
    """
    worktree = _repair_worktree(tmp_path)
    published = _PUBLISHED_PR_HEAD
    advanced = "dd" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{advanced}\n")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=published,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_reconcile_under_lock_require_clean_refuses_dirty_worktree(
    tmp_path: Path,
) -> None:
    worktree = _repair_worktree(tmp_path)
    accepted = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{accepted}\n")
    cmd.queue_result(returncode=0, stdout=" M\0src/a.py\0")
    outcome = await remote_repair_unpublished._reconcile_push_tracking_under_live_equality_lock(
        cmd,
        worktree_path=worktree,
        expected_head=accepted,
        state=state,
        git_env={},
        require_clean=True,
    )
    assert outcome.reconciled is False
    assert outcome.worktree_dirty is True
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_unpublished_post_reset_race_preserves_hosted_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After reset unlocks, a concurrent HEAD advance must not clear hosted orphan markers."""
    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    advanced = "dd" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    cmd.queue_result(returncode=0, stdout=f"{advanced}\n")

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
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_behind_remote_post_reset_race_preserves_hosted_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    advanced = "dd" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{advanced}\n")

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
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_matching_heads_writer_lock_failure_preserves_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _repair_worktree(tmp_path)
    published = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()

    @contextlib.asynccontextmanager
    async def _lock_fails(_path: Path):  # type: ignore[no-untyped-def]
        raise OSError("lock unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(
        remote_repair_unpublished,
        "hold_exclusive_worktree_writer_lock",
        _lock_fails,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, FakeCommandRunner()),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=published,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_matching_heads_live_rev_parse_failure_preserves_push_tracking(
    tmp_path: Path,
) -> None:
    worktree = _repair_worktree(tmp_path)
    published = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="rev-parse failed")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=published,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_unpublished_reconciles_hosted_push_tracking_to_fetched_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted terminal sync + failed push must not leave last_push_sha orphaned.

    Reproduces production: state/last_push_sha=e7, local unpublished e7, fetched
    PR head=5c. After verified abandon reset, push-tracking and next hosted
    identity must advertise 5c (not the abandoned orphan).
    """
    from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace

    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
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
        state=state,
    )
    assert result is None
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False

    workspace = SimpleNamespace(
        repo_url="https://github.com/example/repo",
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        branch_base="main",
        remote_push_branch="awf/ws_repair",
        owned_paths=[],
        task_policy={},
        # Persist may still hold the orphan until the next _persist_state; identity
        # must prefer the reconciled in-memory MonitorState.
        monitor_last_commit_sha=_ORPHANED_HOSTED_TERMINAL,
    )
    identity = hosted_pr_identity_for_workspace(workspace, state=state)
    assert identity["expected_head_sha"] == remote


@pytest.mark.unit
async def test_abandon_unpublished_reconciles_even_when_event_append_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event-append failure must not skip push-tracking reconcile after reset.

    Worktree is already at fetched PR head when events are written. Reconcile
    must run before append so same-cycle hosted identity is correct even if
    append raises. Append failure must still fail the cycle, stash a pending
    audit payload, and durably ``_persist_state`` before returning so a crash
    or finish-op fault cannot lose the retry marker (PRRT_kwDOSJAM6s6dy5TU).
    """
    import json

    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
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

    async def _append_raises(**_kwargs: object) -> None:
        raise RuntimeError("event sink unavailable")

    persisted: list[tuple[str, MonitorState]] = []

    async def _persist_state(workspace_id: str, persist_state: MonitorState) -> None:
        persisted.append((workspace_id, persist_state))

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises
    runner._persist_state = _persist_state

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False
    pending = state.threads_addressed_ids.get(
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    )
    assert pending is not None
    payload = json.loads(pending)
    assert payload["abandoned_local_head"] == local
    assert payload["restored_remote_head"] == remote
    # Stash must be durable before returning; otherwise a crash or
    # ``_finish_monitor_operation`` fault before the outer-loop persist loses the
    # retry marker and the abandonment audit is gone forever.
    assert persisted == [("ws_repair", state)]
    assert remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY in (
        persisted[0][1].threads_addressed_ids
    )


@pytest.mark.unit
async def test_matching_heads_flushes_pending_unpublished_abandon_event(
    tmp_path: Path,
) -> None:
    """Equality short-circuit must retry a stashed abandonment audit event."""
    import json

    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = MonitorState(last_push_sha=remote)
    pending_payload = {
        "abandoned_local_head": local,
        "restored_remote_head": remote,
        "abandoned_paths": ["src/a.py"],
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(pending_payload)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    appended: list[object] = []

    async def _append(*, workspace_id: str, events: list[object]) -> None:
        assert workspace_id == "ws_repair"
        appended.extend(events)

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=remote,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert len(appended) == 1
    event = appended[0]
    assert event.event_type == "monitor.comment_repair_unpublished_abandoned"
    assert event.payload == pending_payload
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        not in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_matching_heads_propagates_pending_abandon_event_flush_failure(
    tmp_path: Path,
) -> None:
    """Failed pending-event flush must not clear the marker or report success."""
    import json

    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    state = MonitorState(last_push_sha=remote)
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(
        {
            "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
            "restored_remote_head": remote,
            "abandoned_paths": ["src/a.py"],
            "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
            "pushed": False,
        }
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")

    async def _append_raises(**_kwargs: object) -> None:
        raise RuntimeError("event sink still unavailable")

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=remote,
        state=state,
    )
    assert restored == remote
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        in state.threads_addressed_ids
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure_kind",
    ["dirty", "head_race", "reset_failure", "verification_failure"],
)
async def test_abandon_unpublished_leaves_push_tracking_on_failed_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    if failure_kind == "verification_failure":
        cmd.queue_result(returncode=1, stderr="verify failed")
        cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        if failure_kind == "dirty":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head=local,
                worktree_dirty=True,
                reset_ok=False,
            )
        if failure_kind == "head_race":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head="aa" * 20,
                worktree_dirty=False,
                reset_ok=False,
            )
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=failure_kind != "reset_failure",
            reset_stderr="reset failed" if failure_kind == "reset_failure" else "",
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
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_behind_remote_ff_reconciles_hosted_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    state = _hosted_orphan_monitor_state()
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
        state=state,
    )
    assert result is None
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False


@pytest.mark.unit
async def test_abandon_behind_remote_ff_flushes_pending_unpublished_abandon_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behind-remote FF success must retry a stashed abandonment audit event.

    Regression for PRRT_kwDOSJAM6s6dzTXE: after an abandon-event failure leaves
    the durable retry marker, a later cycle can take the behind-remote
    fast-forward path (remote advanced) and must flush before returning success
    — otherwise a subsequent repair that clears actionable threads can leave
    the audit permanently un-emitted.
    """
    import json

    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    pending_payload = {
        "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
        "restored_remote_head": remote,
        "abandoned_paths": ["src/a.py"],
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    state = MonitorState(last_push_sha=remote)
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(pending_payload)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")
    appended: list[object] = []

    async def _append(*, workspace_id: str, events: list[object]) -> None:
        assert workspace_id == "ws_repair"
        appended.extend(events)

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
    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert len(appended) == 1
    event = appended[0]
    assert event.event_type == "monitor.comment_repair_unpublished_abandoned"
    assert event.payload == pending_payload
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        not in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_abandon_behind_remote_ff_propagates_pending_abandon_event_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed pending-event flush on FF success must preserve the marker."""
    import json

    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    state = MonitorState(last_push_sha=remote)
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(
        {
            "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
            "restored_remote_head": remote,
            "abandoned_paths": ["src/a.py"],
            "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
            "pushed": False,
        }
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _append_raises(**_kwargs: object) -> None:
        raise RuntimeError("event sink still unavailable")

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
    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert restored == remote
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        in state.threads_addressed_ids
    )


@pytest.mark.unit
@pytest.mark.parametrize("failure_kind", ["dirty", "head_race", "reset_failure"])
async def test_abandon_behind_remote_ff_leaves_push_tracking_on_refused_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        if failure_kind == "dirty":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head=local,
                worktree_dirty=True,
                reset_ok=False,
            )
        if failure_kind == "head_race":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head="dd" * 20,
                worktree_dirty=False,
                reset_ok=False,
            )
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=False,
            reset_stderr="reset failed",
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
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True
