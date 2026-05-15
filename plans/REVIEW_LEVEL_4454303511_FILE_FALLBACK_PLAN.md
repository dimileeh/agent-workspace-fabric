# Review Level 4454303511 File Fallback Plan

## Problem Statement and Scope

Address the review-level feedback for PR comment `issue:4454303511` about
`_classify_setup_dependency_network_result` handling manually constructed
`ValidationCommandResult` instances with partial in-memory captures. The scope
is limited to `src/awf/runtime/validation.py`, a focused regression in
`tests/unit/runtime/test_validation.py`, and this plan/validation record.

## Requirements Checklist

- Add regression coverage for a setup dependency classification result that has
  one captured stream populated and the other stream omitted while the omitted
  stream exists on disk.
- Update stream resolution so each missing captured stream independently falls
  back to its artifact path.
- Preserve existing behavior for `_exec` results that carry both captured
  streams.
- Run the focused regression and the setup dependency classifier test slice.

## Implementation Steps

1. Add a focused unit test that constructs a `ValidationCommandResult` with
   `captured_stdout=""`, `captured_stderr=None`, and a retryable stderr artifact.
2. Confirm the focused test fails with the current all-or-nothing fallback.
3. Update `_classify_setup_dependency_network_result` to resolve stdout and
   stderr independently from capture first, artifact second.
4. Re-run the focused test and the broader setup dependency classifier slice.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "reads_missing_captured_stream_from_artifact"`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "setup_dependency_network_classifier or reads_missing_captured_stream_from_artifact"`
  passes after implementation.
