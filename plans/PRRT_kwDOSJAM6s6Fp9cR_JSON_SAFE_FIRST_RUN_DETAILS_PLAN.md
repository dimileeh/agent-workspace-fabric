# PRRT_kwDOSJAM6s6Fp9cR JSON-Safe First-Run Details Plan

## Problem Statement and Scope

The review thread reports that `render_first_run_json()` calls Pydantic JSON-mode serialization before arbitrary first-run diagnostic `details` values are coerced. Because first-run helpers accept `Mapping[str, Any]`, a setup/start failure can carry a non-Pydantic-serializable object and lose both JSON and pretty output.

Scope is limited to first-run rendering behavior in `src/awf/host_setup/rendering.py`, its focused unit coverage, and this thread's plan/validation artifacts.

## Requirements Checklist

- Add a regression test showing `render_first_run_json()` and `render_first_run_pretty()` tolerate arbitrary non-Pydantic detail values.
- Preserve first-run redaction after arbitrary values are stringified, so secrets embedded in fallback string representations are not emitted.
- Preserve existing JSON/pretty output shape for normal first-run payloads.
- Run only focused validation for the changed rendering behavior; full AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_host_setup_rendering.py` for an arbitrary object in failure details whose string representation includes a token-like value.
2. Confirm the new test fails against the current `model_dump(mode="json")` path.
3. Update `render_first_run_json()` to coerce unknown values with a JSON-safe fallback before existing empty-field cleanup and redaction.
4. Re-run the focused rendering test file or narrower node selection until green.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::<new-test-node> -q`
  - Pass criteria after implementation: the regression passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
  - Pass criteria after implementation: all focused first-run rendering tests pass.

Broad repository validation, coverage gates, frontend builds, and CI-equivalent suites are intentionally not run in this agent phase per the AWF workspace contract.
