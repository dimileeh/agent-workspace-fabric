# Review Comment 4445667428 Secondary Failures Plan

## Problem Statement and Scope

PR review comment `issue:4445667428` flags that preserved failure causality only stores a singleton `secondary_failure`. When a workspace hits multiple recovery or infrastructure failures in the same failure epoch, the newest preserved payload can overwrite the earlier secondary fault evidence.

Scope is limited to failure-causality payload construction and the call sites that build preserved failure payloads for stale-active, runtime-stranding, and cleanup-failure paths. The legacy `secondary_failure` field should remain available as the latest secondary failure for compatibility.

## Requirements

- Add regression coverage proving multiple secondary failures from the same failure epoch are accumulated instead of overwritten.
- Preserve the existing `primary_failure` snapshot behavior and epoch-reset protections.
- Keep `secondary_failure` as the latest secondary failure while adding `secondary_failures` as ordered history.
- Avoid leaking secondary failure history across resumed/remonitored epochs.
- Keep changes scoped to causality helpers, direct callers, and focused tests.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_failure_causality.py` for a preserved event that already has one secondary failure, then records a second secondary failure.
2. Introduce a small failure-causality context loader that returns the primary snapshot plus current-epoch secondary history.
3. Update preserved payload construction to emit `secondary_failures` while retaining `secondary_failure`.
4. Update stale-active, runtime-stranding, and cleanup-failure call sites to pass current-epoch secondary history into the payload builder.
5. Update focused assertions that previously required the singleton-only shape.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_execution_preserves_validation_failure_and_records_secondary_stale tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_runtime_stranding_preserves_provider_auth_primary_failure tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/control/worker.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py tests/unit/control/test_worker.py tests/unit/service/test_controls.py`
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py src/awf/control/worker.py src/awf/service/controls.py`
