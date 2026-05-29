# Review 4571563982 First-Run Rendering Validation

Plan reference: `plans/review_4571563982_first_run_rendering_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving helper-built warning/failure
  payloads without issue details do not emit issue-level `details: {}`.
- Complete: Added a regression test proving helper-built warning/failure
  payloads without next steps do not emit top-level or remediation-level
  `next_steps: []`.
- Complete: Added coverage proving non-empty top-level and remediation
  `next_steps` remain present in rendered JSON.
- Complete: Existing non-empty issue details behavior remains unchanged and
  redacted, covered by the focused warning remediation test.
- Complete: Existing public `redact_first_run_value` tuple behavior is
  preserved; the review's tuple-preservation observation was already addressed
  in the current workspace by `preserve_tuples=True` and its public-pipeline
  regression test.
- Complete: Validation stayed focused. Full AWF/GitHub validation was not run
  inside the agent phase because AWF owns broad validation after completion.

## Evidence

Files changed:

- `src/awf/host_setup/rendering.py`
- `tests/unit/service/test_host_setup_rendering.py`
- `plans/review_4571563982_first_run_rendering_PLAN.md`
- `plans/review_4571563982_first_run_rendering_VALIDATION.md`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_and_failure_payloads_omit_empty_issue_details -q`
  - Pass: `1 passed in 0.46s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_payload_includes_structured_remediation -q`
  - Pass: `1 passed in 0.46s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`
  - Pass: `1 passed in 0.45s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_optional_collections tests/unit/service/test_host_setup_rendering.py::test_first_run_json_preserves_non_empty_remediation_next_steps tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_and_failure_payloads_omit_empty_issue_details tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_payload_includes_structured_remediation tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`
  - Pass: `5 passed in 0.44s`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `All checks passed!`
- `git diff --check`
  - Pass: no whitespace errors.

Initial regression evidence:

- The new empty issue details test failed before the renderer change because
  `rendered_json["issues"][0]` still contained `details: {}`.
- The new empty optional collections test failed before the renderer change
  because `rendered_json` still contained top-level `next_steps: []`.

## Gaps

None.
