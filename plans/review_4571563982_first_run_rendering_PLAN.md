# Review 4571563982 First-Run Rendering Plan

## Problem Statement and Scope

Address the review-level feedback for PR comment `issue:4571563982` in the
first-run rendering contract. The concrete remaining behavior to verify is that
JSON rendering strips empty issue-level `details: {}` in the same way it strips
empty top-level `details`. Also verify whether the tuple-preservation feedback
is already handled by existing code and tests.

Scope is limited to first-run rendering behavior, focused tests, and this plan
and validation record.

## Requirements Checklist

- Add or update a regression test proving helper-built warning/failure payloads
  without issue details do not emit issue-level `details: {}`.
- Keep non-empty issue details in warning/failure payloads unchanged and
  redacted.
- Preserve the existing public `redact_first_run_value` tuple behavior.
- Do not broaden validation beyond focused tests; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a focused failing test in
   `tests/unit/service/test_host_setup_rendering.py` for a helper-built payload
   with no `details`.
2. Update `render_first_run_json` to remove empty `details` from issue
   dictionaries after `model_dump`.
3. Run focused tests for the new regression, existing warning details behavior,
   and tuple-preservation behavior.
4. Record validation evidence in
   `plans/review_4571563982_first_run_rendering_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_and_failure_payloads_omit_empty_issue_details -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_payload_includes_structured_remediation -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`

Pass criteria: all focused tests pass; no broad AWF/GitHub-owned validation is
run inside the agent phase.
