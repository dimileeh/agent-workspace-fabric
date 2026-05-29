# Review 4571563982 First-Run Rendering Plan

## Problem Statement and Scope

Address the review-level feedback for PR comment `issue:4571563982` in the
first-run rendering contract. The prior pass verified that JSON rendering strips
empty issue-level `details: {}` in the same way it strips empty top-level
`details`. The current remaining behavior is that JSON rendering should also
strip empty top-level `next_steps: []` and empty remediation-level
`next_steps: []` so absence checks are consistent for empty optional
collections.

A follow-up review on the same comment identified one more empty optional value:
`remediation.related_command` can be an empty string when a future catalog entry
or explicit override provides `""`. Pretty rendering treats that as absent, so
JSON rendering should strip it as well for contract consistency.

Scope is limited to first-run rendering behavior, focused tests, and this plan
and validation record.

## Assumptions/Changes

The current review pass on the same comment id adds two follow-up observations:

- Successful JSON payloads intentionally keep `issues: []` for stable consumer
  shape even though other empty optional fields are omitted. Existing tests
  assert this contract, so this pass should document the exemption in code
  rather than removing the field.
- Pretty rendering currently formats list/tuple values inside `details` as
  one-line JSON. It should render sequence items across indented lines and
  recurse into nested mappings/sequences so richer first-run details stay
  readable.

## Requirements Checklist

- Add or update a regression test proving helper-built warning/failure payloads
  without issue details do not emit issue-level `details: {}`.
- Add or update a regression test proving helper-built warning/failure payloads
  without next steps do not emit top-level or remediation-level
  `next_steps: []`.
- Keep non-empty top-level and remediation next steps unchanged.
- Keep non-empty issue details in warning/failure payloads unchanged and
  redacted.
- Preserve the existing public `redact_first_run_value` tuple behavior.
- Add a regression test proving empty remediation `related_command: ""` is
  omitted from JSON while pretty output remains absent.
- Keep non-empty remediation `related_command` unchanged.
- Preserve the stable success JSON shape with `issues: []` and add an inline
  code comment explaining why it is exempt from empty-field omission.
- Add a focused regression test proving list/tuple `details` values render as
  indented pretty lines, including nested mappings and redacted values.
- Do not broaden validation beyond focused tests; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a focused failing test in
   `tests/unit/service/test_host_setup_rendering.py` for a helper-built payload
   with no `details` and no `next_steps`.
2. Add coverage for a direct payload whose remediation has non-empty
   `next_steps`, ensuring populated steps remain in JSON.
3. Update `render_first_run_json` to remove empty `details` and empty
   `next_steps` from the relevant dictionaries after `model_dump`.
4. Add a focused failing test for empty remediation `related_command: ""`.
5. Update `render_first_run_json` to strip empty remediation
   `related_command` after `model_dump`.
6. Add a code comment in `render_first_run_json` explaining the intentional
   `issues: []` success shape.
7. Add a focused failing test for structured pretty rendering of sequence
   values under `details`.
8. Update `_render_mapping_lines` to recurse through list/tuple values and
   render sequence items on indented lines while preserving scalar formatting.
9. Run focused tests for the new regression, existing warning details behavior,
   non-empty next-step preservation, and tuple-preservation behavior.
10. Record validation evidence in
   `plans/review_4571563982_first_run_rendering_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_and_failure_payloads_omit_empty_issue_details -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_optional_collections -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_preserves_non_empty_remediation_next_steps -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_payload_includes_structured_remediation -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_remediation_related_command -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_renders_sequence_details_as_nested_lines -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
- `uv run --python 3.12 --extra dev mypy src/awf/host_setup/rendering.py`
- `git diff --check`

Pass criteria: all focused tests pass; no broad AWF/GitHub-owned validation is
run inside the agent phase.
