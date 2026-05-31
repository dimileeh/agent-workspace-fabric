# PRRT_kwDOSJAM6s6F8MC0 Provider Inference Plan

## Problem Statement and Scope

The review thread reports that `infer_provider()` can classify Cursor failures as
Google when Cursor output also includes broad Google quota markers such as
`resource_exhausted`. Scope is limited to provider inference ordering and a
focused regression test.

## Requirements Checklist

- Add a regression test showing Cursor-specific markers take precedence over
  broad Google markers.
- Keep existing Google inference behavior intact.
- Make the smallest implementation change needed in
  `src/awf/adapters/provider_failures.py`.
- Run focused local validation only; full AWF/GitHub validation is managed after
  agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/adapters/test_provider_failures.py`.
2. Run the focused test to confirm the current misclassification.
3. Reorder provider inference so `_CURSOR_MARKERS` are evaluated before
   `_GOOGLE_MARKERS`.
4. Rerun the focused provider failure adapter tests.
5. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py::test_cursor_provider_inference_takes_precedence_over_google_markers -q`
  - First run should fail before the implementation change.
  - Final run should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py -q`
  - Final focused adapter test file should pass.
