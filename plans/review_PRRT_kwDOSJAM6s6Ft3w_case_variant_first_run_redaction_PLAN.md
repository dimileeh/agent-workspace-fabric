# Case-Variant First-Run Redaction Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6Ft3w-` reports that first-run setup/start rendering can leak case-variant secret-looking tokens such as `GHP_...`, `XOXB-...`, or `SK-proj-...` because it relies on the audit redactor's case-sensitive known-token regex before provider-ref redaction.

Scope is limited to first-run rendering and its focused unit coverage. Do not broaden audit redaction semantics unless the first-run boundary cannot be fixed locally.

## Requirements Checklist

- Add a regression test showing first-run JSON output redacts case-variant token prefixes.
- Add a regression assertion showing pretty output redacts the same case-variant tokens.
- Preserve provider-ref redaction and mapping-key collision behavior.
- Keep validation focused to the changed unit test surface; broad AWF/GitHub validation is handled after agent completion.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_host_setup_rendering.py`.
2. Implement a first-run-specific case-insensitive token text redaction pass in `src/awf/host_setup/rendering.py`.
3. Ensure the helper applies to scalar strings and mapping keys before JSON and pretty rendering.
4. Run the targeted host setup rendering tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`

Pass criteria: the focused test file passes, and raw case-variant token values are absent from rendered JSON and pretty output.
