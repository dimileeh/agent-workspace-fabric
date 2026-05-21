# Review 4491715538 Classifier Edges Validation

Plan reference: `plans/review_4491715538_classifier_edges_PLAN.md`

## Requirement Status

- Complete: Allowed added informational jobs to use string-form
  `permissions: read-all`.
- Complete: Preserved the existing block for string-form
  `permissions: write-all`.
- Complete: Allowed unambiguous same-label prerelease bumps such as `rc1` to
  `rc2`, `beta2` to `beta3`, and `alpha3` to `alpha4`.
- Complete: Preserved prerelease downgrade blocking such as `rc10` to `rc2`.
- Complete: Allowed safe `peter-evans/create-or-update-comment` input
  `reactions-edit-mode`.
- Complete: Preserved the existing block for `body-path`.
- Complete: Kept arbitrary step output expressions blocked in
  informational/comment text.
- Complete: Kept coverage `fail_under` raises blocked without protected-file
  ownership.
- Complete: Prepared a local commit on the existing AWF branch without pushing
  or switching branches.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/review_4491715538_classifier_edges_PLAN.md`
- `plans/review_4491715538_classifier_edges_VALIDATION.md`

Tests confirming regressions failed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_restricted_permissions_is_allowed tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_simple_prerelease_bump_is_allowed tests/unit/control/test_quality_gates.py::test_added_comment_action_step_with_reactions_edit_mode_is_allowed -q`
  failed for `permissions: read-all`, the simple prerelease bumps, and
  `reactions-edit-mode`.

Verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_restricted_permissions_is_allowed tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_simple_prerelease_bump_is_allowed tests/unit/control/test_quality_gates.py::test_added_comment_action_step_with_reactions_edit_mode_is_allowed -q`
  passed: 7 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed: 294 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed after formatting.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Remaining Gaps

None for the planned requirements. The review text also mentioned allowing
`write-all`, `body-path`, arbitrary `steps.<id>.outputs.<key>` expressions, and
coverage `fail_under` raises; those remain intentionally blocked because
existing safety regressions treat them as protected policy changes or potential
secret/file-content disclosure paths.
