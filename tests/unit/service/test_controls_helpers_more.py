"""Direct unit tests for ``controls_helpers`` pure helpers and docker seams."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from awf.db.enums import OperationType
from awf.db.models import Operation
from awf.node.cleanup import WorkspaceCleanupResult, WorkspaceCleanupStepResult
from awf.service import controls_helpers
from awf.service.controls_errors import WorkspaceStackStopError
from awf.service.controls_helpers import (
    _claim_lease_is_live,
    _cleanup_failure_message,
    _cleanup_optional_string,
    _cleanup_reason_code,
    _cleanup_result_from_mapping,
    _cleanup_status,
    _cleanup_step_status,
    _cleanup_steps_from_mapping,
    _cleanup_string,
    _communicate,
    _docker_process,
    _is_pr_monitor_recovery_operation,
    _normalize_cleanup_result,
    _payload_matches_idempotency_identity,
    stop_project_containers,
)


class _FakeProcess:
    """Minimal asyncio subprocess stand-in driven by canned communicate output."""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_exec(
    monkeypatch: pytest.MonkeyPatch,
    queue: list[_FakeProcess],
    calls: list[tuple[str, ...]],
) -> None:
    async def _fake_create_subprocess_exec(
        program: str,
        *args: str,
        **_kwargs: object,
    ) -> _FakeProcess:
        calls.append((program, *args))
        return queue.pop(0)

    monkeypatch.setattr(
        controls_helpers.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )


# ---------------------------------------------------------------------------
# stop_project_containers / _docker_process / _communicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stop_project_containers_noop_for_blank_project() -> None:
    # Blank/None project name short-circuits before touching docker at all.
    assert await stop_project_containers(None) is None
    assert await stop_project_containers("") is None


@pytest.mark.unit
async def test_stop_project_containers_runs_ps_then_stop_with_container_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    queue = [
        _FakeProcess(stdout=b"abc123\n\n  def456  \n"),
        _FakeProcess(stdout=b""),
    ]
    _patch_exec(monkeypatch, queue, calls)

    await stop_project_containers("awf_demo")

    # ps lists running container ids; whitespace/blank lines are stripped, and
    # the survivors are forwarded verbatim to ``docker stop``.
    assert calls == [
        (
            "docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=awf_demo",
        ),
        ("docker", "stop", "abc123", "def456"),
    ]


@pytest.mark.unit
async def test_stop_project_containers_skips_stop_when_no_running_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    queue = [_FakeProcess(stdout=b"   \n\n")]
    _patch_exec(monkeypatch, queue, calls)

    await stop_project_containers("awf_empty")

    # Only ``docker ps`` runs; with no ids there is nothing to stop.
    assert [call[1] for call in calls] == ["ps"]


@pytest.mark.unit
async def test_stop_project_containers_raises_when_stop_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    queue = [
        _FakeProcess(stdout=b"abc123\n"),
        _FakeProcess(stderr=b"permission denied", returncode=17),
    ]
    _patch_exec(monkeypatch, queue, calls)

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await stop_project_containers("awf_denied")

    error = exc_info.value
    assert error.operation == "stop"
    assert error.returncode == 17
    assert error.stderr == "permission denied"
    assert error.error_code == "STACK_STOP_FAILED"


@pytest.mark.unit
async def test_communicate_returns_decoded_streams_on_success() -> None:
    proc = _FakeProcess(stdout=b"hello\n", stderr=b"warn", returncode=0)

    stdout, stderr = await _communicate(proc, operation="ps")

    assert stdout == "hello\n"
    assert stderr == "warn"


@pytest.mark.unit
async def test_communicate_replaces_invalid_utf8_and_raises_on_failure() -> None:
    proc = _FakeProcess(stdout=b"\xff", stderr=b"boom", returncode=2)

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await _communicate(proc, operation="stop")

    error = exc_info.value
    assert error.returncode == 2
    # Undecodable bytes are replaced rather than raising during decode.
    assert error.stdout == "�"
    assert error.stderr == "boom"


@pytest.mark.unit
async def test_docker_process_missing_executable_maps_to_stack_stop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_not_found(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(
        controls_helpers.asyncio,
        "create_subprocess_exec",
        _raise_not_found,
    )

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await _docker_process("ps", operation="ps")

    error = exc_info.value
    assert error.returncode == 127
    assert "docker executable is not available" in error.stderr


@pytest.mark.unit
async def test_docker_process_os_error_maps_to_stack_stop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_oserror(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("no exec")

    monkeypatch.setattr(
        controls_helpers.asyncio,
        "create_subprocess_exec",
        _raise_oserror,
    )

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await _docker_process("stop", "abc", operation="stop")

    error = exc_info.value
    assert error.returncode == 1
    assert error.stderr == "PermissionError: no exec"


@pytest.mark.unit
async def test_docker_process_returns_live_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _FakeProcess(stdout=b"ok")
    calls: list[tuple[str, ...]] = []
    _patch_exec(monkeypatch, [sentinel], calls)

    proc = await _docker_process("ps", "-q", operation="ps")

    assert proc is sentinel
    assert calls == [("docker", "ps", "-q")]


# ---------------------------------------------------------------------------
# _is_pr_monitor_recovery_operation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pr_monitor_recovery_operation_true_for_recovery_validate() -> None:
    operation = Operation(
        type=OperationType.validate.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
    )
    assert _is_pr_monitor_recovery_operation(operation) is True


@pytest.mark.unit
def test_pr_monitor_recovery_operation_false_for_operator_source() -> None:
    operation = Operation(
        type=OperationType.rebase.value,
        payload={"source": "operator_api", "recovery_mode": "rebase_only"},
    )
    assert _is_pr_monitor_recovery_operation(operation) is False


@pytest.mark.unit
def test_pr_monitor_recovery_operation_false_for_non_mapping_payload() -> None:
    # A non-Mapping payload (e.g. legacy list payload) cannot be a recovery op.
    operation = Operation(type=OperationType.validate.value, payload=["legacy"])
    assert _is_pr_monitor_recovery_operation(operation) is False


@pytest.mark.unit
def test_pr_monitor_recovery_operation_false_for_unrelated_type() -> None:
    operation = Operation(
        type=OperationType.cancel.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
    )
    assert _is_pr_monitor_recovery_operation(operation) is False


# ---------------------------------------------------------------------------
# cleanup-result normalization helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_cleanup_result_passes_through_result_instance() -> None:
    result = WorkspaceCleanupResult.skipped(reason_code="WORKSPACE_ALREADY_DESTROYED")
    assert _normalize_cleanup_result(result) is result


@pytest.mark.unit
def test_normalize_cleanup_result_dispatches_mapping_to_from_mapping() -> None:
    mapping = {
        "status": "succeeded",
        "reason_code": "CLEANUP_SUCCEEDED",
        "steps": [
            {
                "name": "compose_down",
                "status": "succeeded",
                "reason_code": "COMPOSE_DOWN_SUCCEEDED",
            }
        ],
    }

    result = _normalize_cleanup_result(mapping)

    assert result.status == "succeeded"
    assert result.reason_code == "CLEANUP_SUCCEEDED"
    assert result.steps == (
        WorkspaceCleanupStepResult(
            name="compose_down",
            status="succeeded",
            reason_code="COMPOSE_DOWN_SUCCEEDED",
        ),
    )


@pytest.mark.unit
def test_normalize_cleanup_result_from_string_failure_sequence() -> None:
    result = _normalize_cleanup_result(["compose down failed", "worktree removal failed"])

    assert result.status == "partial"
    assert [step.name for step in result.steps] == [
        "compose down failed",
        "worktree removal failed",
    ]
    assert all(step.status == "failed" for step in result.steps)
    assert all(step.reason_code == "CLEANUP_STEP_FAILED" for step in result.steps)


@pytest.mark.unit
def test_cleanup_result_from_mapping_falls_back_to_completed_and_failed_steps() -> None:
    # When no top-level ``steps`` list is present, completed + failed step lists
    # are concatenated (completed first) and unknown statuses are coerced.
    mapping = {
        "status": "weird-status",
        "completed_steps": [
            {"name": "compose_down", "status": "succeeded"},
        ],
        "failed_steps": [
            {"status": "failed", "error": "boom"},
            "not-a-mapping-step",
        ],
    }

    result = _cleanup_result_from_mapping(mapping)

    # Unknown overall status coerces to "partial"; missing reason_code derived.
    assert result.status == "partial"
    assert result.reason_code == "CLEANUP_PARTIAL"
    assert [(step.name, step.status, step.reason_code, step.error) for step in result.steps] == [
        ("compose_down", "succeeded", "CLEANUP_STEP_SUCCEEDED", None),
        ("cleanup_step_2", "failed", "CLEANUP_STEP_FAILED", "boom"),
    ]


@pytest.mark.unit
def test_cleanup_steps_from_mapping_uses_explicit_steps_and_preserves_reason_codes() -> None:
    steps = _cleanup_steps_from_mapping(
        {
            "steps": [
                {
                    "name": "compose_down",
                    "status": "succeeded",
                    "reason_code": "COMPOSE_DOWN_SUCCEEDED",
                    "error": "",
                },
                {"status": "failed"},
            ]
        }
    )

    assert steps == [
        WorkspaceCleanupStepResult(
            name="compose_down",
            status="succeeded",
            reason_code="COMPOSE_DOWN_SUCCEEDED",
            error=None,
        ),
        WorkspaceCleanupStepResult(
            name="cleanup_step_2",
            status="failed",
            reason_code="CLEANUP_STEP_FAILED",
            error=None,
        ),
    ]


@pytest.mark.unit
def test_cleanup_steps_from_mapping_ignores_string_step_collections() -> None:
    # A string ``steps`` value is not a step sequence, and string fallback
    # collections are likewise ignored, yielding no steps.
    assert (
        _cleanup_steps_from_mapping(
            {"steps": "compose_down", "failed_steps": "x", "completed_steps": "y"}
        )
        == []
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("succeeded", "succeeded"),
        ("partial", "partial"),
        ("skipped", "skipped"),
        ("unknown", "partial"),
        (None, "partial"),
    ],
)
def test_cleanup_status_coerces_unknown_to_partial(value: object, expected: str) -> None:
    assert _cleanup_status(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("skipped", "skipped"),
        ("bogus", "failed"),
        (123, "failed"),
    ],
)
def test_cleanup_step_status_coerces_unknown_to_failed(value: object, expected: str) -> None:
    assert _cleanup_step_status(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "status", "expected"),
    [
        ("EXPLICIT_CODE", "partial", "EXPLICIT_CODE"),
        ("", "succeeded", "CLEANUP_SUCCEEDED"),
        (None, "skipped", "CLEANUP_SKIPPED"),
        (None, "partial", "CLEANUP_PARTIAL"),
    ],
)
def test_cleanup_reason_code_uses_value_then_status_default(
    value: object, status: str, expected: str
) -> None:
    assert _cleanup_reason_code(value, status=status) == expected


@pytest.mark.unit
def test_cleanup_string_returns_fallback_for_empty_or_nonstring() -> None:
    assert _cleanup_string("name", fallback="fb") == "name"
    assert _cleanup_string("", fallback="fb") == "fb"
    assert _cleanup_string(None, fallback="fb") == "fb"
    assert _cleanup_string(42, fallback="fb") == "fb"


@pytest.mark.unit
def test_cleanup_optional_string_returns_none_for_empty_or_nonstring() -> None:
    assert _cleanup_optional_string("err") == "err"
    assert _cleanup_optional_string("") is None
    assert _cleanup_optional_string(None) is None
    assert _cleanup_optional_string(7) is None


@pytest.mark.unit
def test_cleanup_failure_message_joins_failures_then_falls_back_to_reason_code() -> None:
    failed = WorkspaceCleanupResult(
        status="partial",
        reason_code="CLEANUP_PARTIAL",
        steps=(
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error="compose down failed",
            ),
            WorkspaceCleanupStepResult(
                name="worktree_remove",
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error=None,
            ),
        ),
    )
    assert _cleanup_failure_message(failed) == "compose down failed, worktree_remove"

    # No failed steps -> fall back to the overall reason code.
    no_failures = WorkspaceCleanupResult.skipped(reason_code="WORKSPACE_ALREADY_DESTROYED")
    assert _cleanup_failure_message(no_failures) == "WORKSPACE_ALREADY_DESTROYED"


# ---------------------------------------------------------------------------
# _payload_matches_idempotency_identity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_payload_matches_identity_none_always_matches() -> None:
    # With no identity to compare against, any payload trivially matches.
    assert _payload_matches_idempotency_identity({"anything": 1}, identity=None, identity_keys=None)
    assert _payload_matches_idempotency_identity(["not-a-dict"], identity=None, identity_keys=None)


@pytest.mark.unit
def test_payload_matches_identity_non_dict_payload_never_matches() -> None:
    assert (
        _payload_matches_idempotency_identity(
            ["legacy"],
            identity={"reason_code": "OPERATOR_REBASE"},
            identity_keys=None,
        )
        is False
    )


@pytest.mark.unit
def test_payload_matches_identity_compares_selected_keys() -> None:
    identity = {"reason_code": "OPERATOR_REBASE", "expected_version": 3}
    keys = frozenset({"reason_code", "expected_version"})

    assert _payload_matches_idempotency_identity(
        {"reason_code": "OPERATOR_REBASE", "expected_version": 3, "noise": "x"},
        identity=identity,
        identity_keys=keys,
    )
    assert (
        _payload_matches_idempotency_identity(
            {"reason_code": "OPERATOR_REBASE", "expected_version": 99},
            identity=identity,
            identity_keys=keys,
        )
        is False
    )
    # A key present in identity but missing from the payload is a mismatch.
    assert (
        _payload_matches_idempotency_identity(
            {"reason_code": "OPERATOR_REBASE"},
            identity=identity,
            identity_keys=keys,
        )
        is False
    )


@pytest.mark.unit
def test_payload_matches_identity_skips_keys_absent_from_identity() -> None:
    # ``identity_keys`` may name keys not present in identity; those are skipped.
    assert _payload_matches_idempotency_identity(
        {"reason_code": "OPERATOR_REBASE"},
        identity={"reason_code": "OPERATOR_REBASE"},
        identity_keys=frozenset({"reason_code", "absent_key"}),
    )


@pytest.mark.unit
def test_claim_lease_is_live_false_for_missing_owner_or_expiry() -> None:
    now = datetime.now(UTC)
    assert not _claim_lease_is_live(None, now + timedelta(minutes=5), now=now)
    assert not _claim_lease_is_live("worker", None, now=now)


@pytest.mark.unit
def test_claim_lease_is_live_naive_expiry_against_aware_now() -> None:
    # Naive stored expiry + aware ``now`` (the realistic path) compares naively.
    now = datetime.now(UTC)
    naive_future = now.replace(tzinfo=None) + timedelta(minutes=5)
    assert _claim_lease_is_live("worker", naive_future, now=now)


@pytest.mark.unit
def test_claim_lease_is_live_aware_expiry_against_naive_now() -> None:
    # Symmetric guard: an aware expiry paired with a naive ``now`` must compare
    # naively rather than raising ``TypeError`` on a mixed-awareness comparison.
    aware_now = datetime.now(UTC)
    naive_now = aware_now.replace(tzinfo=None)
    assert _claim_lease_is_live("worker", aware_now + timedelta(minutes=5), now=naive_now)
    assert not _claim_lease_is_live("worker", aware_now - timedelta(minutes=5), now=naive_now)


@pytest.mark.unit
def test_claim_lease_is_live_non_utc_aware_expiry_converts_before_compare() -> None:
    # An aware ``expires_at`` in a non-UTC offset must be converted to UTC before
    # the tz-naive comparison; stripping the offset without converting would
    # treat the local wall-clock as UTC and could mark an expired lease live.
    plus_530 = timezone(timedelta(hours=5, minutes=30))
    aware_now = datetime.now(UTC)
    naive_now = aware_now.replace(tzinfo=None)
    # Same instant as ``naive_now`` but expressed in +05:30 — its naive
    # wall-clock reads ~5.5h ahead, so a non-converting strip would wrongly
    # report it as live.
    expired_other_zone = (aware_now - timedelta(minutes=5)).astimezone(plus_530)
    assert not _claim_lease_is_live("worker", expired_other_zone, now=naive_now)
    live_other_zone = (aware_now + timedelta(minutes=5)).astimezone(plus_530)
    assert _claim_lease_is_live("worker", live_other_zone, now=naive_now)
