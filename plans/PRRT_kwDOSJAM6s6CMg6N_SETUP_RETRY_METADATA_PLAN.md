# PRRT_kwDOSJAM6s6CMg6N Setup Retry Metadata Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6CMg6N` reports that when a setup command first
hits a transient dependency network failure, consumes a setup retry, and then
fails for a different non-flaky reason, the final command result drops
`setup_dependency_network` metadata. The executor records durable setup retry
events only from that metadata, so the real retry is lost.

Scope is limited to preserving setup retry metadata on later terminal failures
and ensuring executor terminal failure classification still reflects the actual
final failure.

## Requirements Checklist

- [ ] Add a runtime regression for setup dependency retry followed by a
      deterministic setup failure.
- [ ] Preserve `setup_dependency_network` metadata and attempt lineage on the
      later final failure.
- [ ] Keep the final command reason code as the actual later failure
      (`COMMAND_FAILED` / retry exhaustion), not
      `SETUP_DEPENDENCY_NETWORK_FAILURE`.
- [ ] Add executor coverage proving the preserved metadata emits the retry event
      without marking the terminal workspace failure as setup dependency
      exhaustion.
- [ ] Run focused runtime and executor tests plus lint on touched files.

## Implementation Steps

1. Update the existing mixed-attempt runtime test so it expects preserved setup
   retry metadata while still asserting the final reason remains
   `COMMAND_FAILED`.
2. Add an executor regression with a failed setup result that carries
   non-exhausted setup dependency metadata.
3. Attach prior setup dependency metadata to final failure results when earlier
   setup dependency attempts exist and the retry path did not recover or exhaust.
4. Adjust executor setup failure details so only a terminal
   `SETUP_DEPENDENCY_NETWORK_FAILURE` drives the workspace terminal reason and
   details.
5. Run the focused pytest targets and ruff check.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_preserves_metadata_when_later_failure_reclassifies -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestExecutorCoverageEdges::test_executor_setup_dependency_retry_then_later_setup_failure_records_retry_without_terminal_setup_reason -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py src/awf/control/executor.py tests/unit/runtime/test_validation.py tests/unit/control/test_executor_error_paths.py
```

Pass criteria: the new regressions pass, ruff reports no issues, and changed
behavior is limited to metadata/event preservation for mixed setup retry
outcomes.
