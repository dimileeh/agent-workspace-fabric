# PRRT_kwDOSJAM6s6FmeBQ Tuple Redaction Plan

## Problem Statement and Scope

The review thread reports that `_redact_provider_refs` converts tuples to lists while recursively redacting first-run payload values. Lists should remain lists, but tuple inputs should preserve tuple shape to avoid downstream type mismatches for callers that pass raw values through `redact_first_run_value`.

Scope is limited to `src/awf/host_setup/rendering.py`, the focused host setup rendering tests, and this plan/validation evidence. The shared audit redactor's JSON-safe tuple normalization is out of scope.

## Requirements Checklist

- Add a regression test proving tuple inputs remain tuples in `_redact_provider_refs`.
- Preserve existing list behavior and redaction of nested provider refs.
- Change `_redact_provider_refs` so tuple inputs return tuples.
- Run only focused validation owned by this change; full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Update `tests/unit/service/test_host_setup_rendering.py` with a focused tuple-preservation regression around `_redact_provider_refs`.
2. Run the focused test and confirm it fails against the current tuple-to-list behavior when practical.
3. Update `src/awf/host_setup/rendering.py` to return a tuple for tuple input.
4. Re-run the focused host setup rendering test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`

Pass criteria: the focused test file passes, including the new tuple-preservation regression. Broad AWF/GitHub validation is intentionally not run inside this agent phase per workspace contract.
