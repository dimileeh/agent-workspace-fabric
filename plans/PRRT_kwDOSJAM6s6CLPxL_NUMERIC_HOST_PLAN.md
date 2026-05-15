# PRRT_kwDOSJAM6s6CLPxL Numeric Host Plan

## Problem Statement and Scope

The setup dependency network classifier falls back to extracting dotted tokens as
hosts when no URL is present. Review thread `PRRT_kwDOSJAM6s6CLPxL` reports that
pure version strings such as `1.2.3` can be recorded as host metadata. This fix
is scoped to preventing numeric dotted fallback host candidates while preserving
real host extraction.

## Requirements Checklist

- Add a regression test showing a dependency setup network failure with only a
  version-like dotted token does not classify that token as a host.
- Preserve existing URL hostname extraction and fallback extraction for real
  hostnames.
- Keep the change local to setup dependency network classification.
- Run the narrow unit test coverage for the touched runtime validation behavior.
- Create validation documentation for this plan.

## Implementation Steps

1. Add a focused failing unit test in `tests/unit/runtime/test_validation.py`.
2. Update `_extract_setup_dependency_host` in `src/awf/runtime/validation.py` to
   skip fallback candidates made only of digits and dots.
3. Run the targeted test file or narrower test selection.
4. Record results in `plans/PRRT_kwDOSJAM6s6CLPxL_NUMERIC_HOST_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`

Pass criteria: the runtime validation tests pass, including the new regression,
and no unrelated files are modified.
