# PRRT_kwDOSJAM6s6Fox3b Branch Manifest Provenance Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6Fox3b_branch_manifest_provenance_PLAN.md`

## Requirement Status

- Complete: Preserved normal local manifest generation.
  - Evidence: the focused manifest-generator test file passes.
- Complete: Preserved GitHub Actions tag-ref manifest generation for the exact
  release tag.
  - Evidence: existing tag-ref tests in
    `tests/unit/scripts/test_generate_install_manifest.py` pass.
- Complete: Manual `workflow_dispatch` branch refs are allowed only when the
  requested release tag resolves locally to the same commit as `GITHUB_SHA`.
  - Evidence:
    `test_manifest_generator_allows_workflow_dispatch_branch_ref_with_matching_tag`
    creates a local tag at `HEAD` and verifies manifest output.
- Complete: Manual branch dispatches skip and remove stale output when tag
  provenance is missing or mismatched.
  - Evidence:
    `test_manifest_generator_skips_workflow_dispatch_branch_ref_without_verified_tag`
    and
    `test_manifest_generator_skips_workflow_dispatch_branch_ref_with_mismatched_tag`
    cover unverifiable and wrong-commit tags.
- Complete: Non-dispatch branch refs remain skipped.
  - Evidence: existing non-dispatch branch-ref tests pass.
- Complete: Used focused validation only.
  - Evidence: no full repository tests, coverage gates, frontend builds,
    OpenAPI drift checks, or CI-equivalent validation were run.

## Evidence

Changed files:

- `scripts/generate_install_manifest.py`
- `tests/unit/scripts/test_generate_install_manifest.py`
- `plans/review_PRRT_kwDOSJAM6s6Fox3b_branch_manifest_provenance_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6Fox3b_branch_manifest_provenance_VALIDATION.md`

Commands run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_generator_skips_workflow_dispatch_branch_ref_without_verified_tag -q`
  failed because the current implementation wrote
  `awf-install-manifest.json` instead of printing `SKIP`.
- Focused branch-dispatch regressions:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k "workflow_dispatch_branch_ref"`
  passed with `3 passed, 41 deselected`.
- Focused manifest tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  passed with `44 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  passed with `All checks passed!`.

Full AWF/GitHub validation, coverage gates, frontend builds, OpenAPI drift
checks, and CI-equivalent validation remain managed by AWF/GitHub after this
agent completes.

## Remaining Gaps

None.
