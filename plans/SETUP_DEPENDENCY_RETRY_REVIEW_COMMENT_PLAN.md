# Setup Dependency Retry Review Comment Plan

## Problem statement and scope
Address PR review comment `issue:4454303511` for setup dependency retry observability. Scope is limited to artifact retry-prefix timestamping and clarifying retry-event payload semantics when a setup dependency retry is followed by a non-transient terminal setup failure.

## Requirements checklist
- Add elapsed monotonic timing to setup dependency retry output prefixes written into stdout/stderr artifact files.
- Preserve existing retry metadata and reason-code behavior, including the existing `failure_reason_code` for retry events after later deterministic failure.
- Add a code comment near setup dependency retry event payload construction explaining that `failure_reason_code` identifies the retry classifier, while terminal workspace failure may be different when `retry_exhausted=false` and `recovered=false`.
- Add or update focused regression coverage for the artifact prefix timestamp.
- Run the narrow validation tests that cover runtime validation retry behavior and executor retry event behavior.

## Implementation steps
1. Add a failing runtime validation test asserting setup dependency retry artifact prefixes include elapsed seconds.
2. Update `_setup_dependency_retry_output_prefix` to accept elapsed seconds and format the artifact prefix with that value.
3. Thread a monotonic setup phase start value through the retry loop when constructing retry prefixes.
4. Add the executor payload semantics comment without changing existing payload fields.
5. Run targeted tests and record evidence in the validation document.

## Verification commands and pass criteria
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  - Passes, including the new artifact-prefix timestamp assertion.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestExecutorCoverageEdges::test_executor_setup_dependency_retry_then_later_setup_failure_records_retry_without_terminal_setup_reason -q`
  - Passes, proving the documented event semantics remain covered.
