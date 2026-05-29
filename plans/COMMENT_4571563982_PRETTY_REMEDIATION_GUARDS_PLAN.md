# Comment 4571563982 Pretty/Remediation Guards Plan

## Problem Statement and Scope

Address the review-level hardening feedback in PR comment `issue:4571563982` for first-run rendering:

- `_format_pretty_value()` should render non-finite floats through the same stable string form used by JSON-safe first-run payloads when private pretty helpers are called directly.
- `first_run_remediation_from_reason_code()` should reject catalog entries whose resolved first-run remediation fields are incomplete with a clear `ValueError`, instead of surfacing a Pydantic validation error.

Scope is limited to `src/awf/host_setup/rendering.py`, focused unit coverage in `tests/unit/service/test_host_setup_rendering.py`, this plan, and the matching validation document.

## Requirements Checklist

- Add focused regression coverage for direct pretty formatting of `nan`, `inf`, and `-inf`.
- Add focused regression coverage that an OK/status-only catalog entry such as `DOCKER_OK` raises `ValueError` from the first-run remediation helper.
- Keep valid first-run reason rendering behavior unchanged.
- Run only targeted host setup rendering tests; broad AWF/GitHub validation remains managed after agent completion.
- Commit the local fix on the current AWF-managed branch without pushing or switching branches.

## Implementation Steps

1. Add failing tests for non-finite pretty scalar formatting and incomplete catalog remediation rejection.
2. Update `_format_pretty_value()` to return `str(value)` for non-finite floats before calling `json.dumps()`.
3. Resolve remediation fields before model construction and raise `ValueError` if any required first-run remediation field is blank after stripping.
4. Run the targeted tests that cover the new regressions and existing first-run rendering behavior.
5. Record verification in `plans/COMMENT_4571563982_PRETTY_REMEDIATION_GUARDS_VALIDATION.md`.
6. Stage only changed files and commit with a conventional commit message for comment `4571563982`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_format_pretty_value_formats_non_finite_floats_as_strings tests/unit/service/test_host_setup_rendering.py::test_first_run_remediation_rejects_catalog_entries_without_guidance -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`

Pass criteria: targeted rendering tests pass locally. Full AWF/GitHub validation, broad lint/type checks, full repository tests, and coverage gates are intentionally left to AWF after agent completion per workspace contract.
