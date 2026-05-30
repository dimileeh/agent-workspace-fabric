# Comment 3325438682 Non-Finite JSON Plan

## Scope

Address PR review thread `PRRT_kwDOSJAM6s6Fuk5O` on
`src/awf/host_setup/rendering.py`, which reports that first-run JSON rendering
can pass through `NaN` and infinity float values from diagnostic details.

## Plan

1. Add a focused regression test in `tests/unit/service/test_host_setup_rendering.py`
   showing that rendered first-run JSON containing non-finite floats can be
   serialized with `json.dumps(..., allow_nan=False)`.
2. Update `src/awf/host_setup/rendering.py` so `_json_safe_first_run_value()`
   preserves finite floats but normalizes non-finite floats to JSON-safe string
   diagnostics.
3. Run the narrow rendering test that proves this behavior. Full AWF/GitHub
   validation remains owned by AWF after agent completion.
4. Record validation evidence in a matching validation document and commit the
   scoped fix locally.
