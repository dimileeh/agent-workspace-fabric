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
- Complete: Added a regression test proving empty remediation
  `related_command: ""` is omitted from JSON while pretty output remains absent.
- Complete: Existing non-empty remediation `related_command` behavior remains
  unchanged, covered by the failure payload test.
- Complete: The stable success JSON shape still keeps `issues: []`, and
  `render_first_run_json` now documents why that field is exempt from empty
  optional-field omission.
- Complete: Pretty rendering now expands non-empty list/tuple values in
  `details` into indented sequence lines, recursing into nested mappings and
  preserving redaction.
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
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_remediation_related_command -q`
  - Initial failure before the renderer change: JSON still contained
    `related_command: ""`.
  - Pass after the renderer change: `1 passed in 0.43s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_optional_collections tests/unit/service/test_host_setup_rendering.py::test_first_run_json_preserves_non_empty_remediation_next_steps tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_remediation_related_command tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_payload_includes_structured_remediation tests/unit/service/test_host_setup_rendering.py::test_first_run_failure_payload_includes_reason_and_safe_details tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`
  - Pass: `6 passed in 0.42s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_renders_sequence_details_as_nested_lines -q`
  - Initial failure before the renderer change: pretty output still rendered
    `paths: ["[redacted]"]` and `port_conflicts: [...]` inline.
  - Pass after the renderer change: `1 passed in 0.41s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_success_payload_renders_pretty_and_json tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_optional_collections tests/unit/service/test_host_setup_rendering.py::test_first_run_json_preserves_non_empty_remediation_next_steps tests/unit/service/test_host_setup_rendering.py::test_first_run_json_omits_empty_remediation_related_command tests/unit/service/test_host_setup_rendering.py::test_first_run_warning_payload_includes_structured_remediation tests/unit/service/test_host_setup_rendering.py::test_first_run_failure_payload_includes_reason_and_safe_details tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_renders_sequence_details_as_nested_lines tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`
  - Pass: `8 passed in 0.44s`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `2 files already formatted`
- `uv run --python 3.12 --extra dev mypy src/awf/host_setup/rendering.py`
  - Pass: `Success: no issues found in 1 source file`
- `git diff --check`
  - Pass: no whitespace errors.

Initial regression evidence:

- The new empty issue details test failed before the renderer change because
  `rendered_json["issues"][0]` still contained `details: {}`.
- The new empty optional collections test failed before the renderer change
  because `rendered_json` still contained top-level `next_steps: []`.

## Iteration 2

Follow-up requirements from the same review-level comment:

- Complete: Added a regression test proving empty nested mapping values in
  issue `details` render as `key: {}` instead of orphaned header-only pretty
  lines.
- Complete: Added a regression test proving provider-reference key redaction
  requires an explicit `credential_ref(s)` / `provider_ref(s)` key with `_` or
  `-` separators, avoiding no-separator and mid-key substring matches while
  preserving supported key redaction.
- Complete: Kept validation focused. Full AWF/GitHub validation was not run
  inside the agent phase because AWF owns broad validation after completion.

Iteration 2 evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_renders_empty_nested_mapping_details_as_scalar tests/unit/service/test_host_setup_rendering.py::test_provider_ref_key_redaction_requires_explicit_ref_key -q`
  - Initial failure before the renderer changes: empty nested mappings rendered
    as header-only lines, and `credentialref`, `providerref`,
    `last_credential_ref_update`, and `provider_ref_hint` were over-redacted.
  - Pass after the renderer changes: `2 passed in 0.42s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_renders_sequence_details_as_nested_lines tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_renders_empty_nested_mapping_details_as_scalar tests/unit/service/test_host_setup_rendering.py::test_first_run_failure_payload_includes_reason_and_safe_details tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_tokens_provider_refs_and_sensitive_keys tests/unit/service/test_host_setup_rendering.py::test_provider_ref_redaction_preserves_tuple_container_type tests/unit/service/test_host_setup_rendering.py::test_provider_ref_key_redaction_requires_explicit_ref_key tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_does_not_double_redact_provider_ref_assignments -q`
  - Pass: `7 passed in 0.44s`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `2 files already formatted`
- `uv run --python 3.12 --extra dev mypy src/awf/host_setup/rendering.py`
  - Pass: `Success: no issues found in 1 source file`
- `git diff --check`
  - Pass: no whitespace errors.

## Gaps

None.
