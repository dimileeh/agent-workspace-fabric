# Review 4491715538 SHA Ref Validation

Plan reference: `plans/REVIEW_4491715538_SHA_REF_PLAN.md`

## Requirement Status

- Complete: Preserved the existing allowance for moving from a mutable version
  tag to a pinned SHA.
- Complete: Blocked moving from a pinned SHA to a mutable major version tag with
  `test_workflow_pinned_uses_sha_to_mutable_major_tag_is_blocked`.
- Complete: Allowed moving from a pinned SHA to a full semver version ref with
  `test_workflow_pinned_uses_sha_to_full_semver_is_allowed`.
- Complete: Preserved the existing allowance for non-downgrade full-version
  bumps.
- Complete: Kept the `continue-on-error` strictness-improvement behavior
  unchanged; the full quality-gate module includes those existing regressions.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_SHA_REF_PLAN.md`
- `plans/REVIEW_4491715538_SHA_REF_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "sha_to_mutable_major_tag or sha_to_full_semver"` failed before implementation with the expected SHA-to-`v4` regression failure.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "sha_to_mutable_major_tag or sha_to_full_semver"` passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q` passed with 41 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.
