# PRRT_kwDOSJAM6s6Fh8ee Expanded Resolve Fallback Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6Fh8ee` reports that
`src/awf/host_setup/source_assets.py::_resolve_candidate` expands a candidate
path before `resolve()`, but falls back to the original unexpanded path when
`resolve()` raises. For `~`-prefixed source checkout paths, this can report a
literal `~` component in diagnostics instead of the expanded home path.

Scope is limited to `_resolve_candidate` fallback behavior and a focused
regression in the host setup source-checkout tests.

## Requirements Checklist

- Add a regression proving a `~`-prefixed candidate uses the expanded path when
  `resolve()` raises `OSError`.
- Preserve the existing fallback for `expanduser()` failures, which must keep
  the original absolute candidate because no expanded path is available.
- Keep source-checkout failures reason-coded through `SourceCheckoutError`.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add the focused failing regression in
   `tests/unit/service/test_host_setup_config.py`.
2. Confirm the regression fails against the current implementation.
3. Change `_resolve_candidate` to fall back to the expanded path when expansion
   succeeds and resolution fails.
4. Re-run the focused regression and nearby source-checkout path fallback tests.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "resolve_failure or expanduser_failure"`
  - Passes with the new regression and existing expanduser failure regression.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  - Passes without lint errors.

Full AWF/GitHub validation remains managed by AWF after agent completion.
