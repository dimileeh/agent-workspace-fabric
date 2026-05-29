# PRRT_kwDOSJAM6s6Ffp Expanduser Fallback Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6Ffp_-` reports that
`src/awf/host_setup/source_assets.py::_resolve_candidate` calls
`Path.expanduser()` before its fallback `try` block. If home resolution is
unavailable, `expanduser()` can raise `RuntimeError` and bypass the
reason-coded source-checkout validation path.

Scope is limited to preserving fallback behavior for candidate path resolution
and adding a focused regression in the host setup source-checkout tests.

## Requirements Checklist

- Add a regression proving an `expanduser()` `RuntimeError` falls back to the
  non-expanded absolute path instead of escaping.
- Keep existing `resolve()` `OSError` fallback behavior intact.
- Keep diagnostics flowing through existing source-checkout validation behavior.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add the focused failing regression in `tests/unit/service/test_host_setup_config.py`.
2. Confirm the regression fails against the current implementation.
3. Move `expanduser()` inside `_resolve_candidate`'s `try` block and catch
   `RuntimeError` together with `OSError`, returning the base path's absolute
   form on fallback.
4. Re-run the focused regression and related host setup tests.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "expanduser or valid_source_checkout or invalid_source_checkout or stale_source_checkout or unreadable_source_checkout"`
  - Passes with the new regression and representative source-checkout coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  - Passes without lint errors.

Full AWF/GitHub validation remains owned by AWF after this agent phase.
