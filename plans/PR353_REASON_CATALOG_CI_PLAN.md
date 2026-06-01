# PR 353 Reason Catalog CI Plan

## Problem Statement and Scope

PR #353 fails the focused catalog coverage check because the public reason code
`MERGE_METHOD_MISMATCH` is emitted by the PR monitor merge-method path but is
missing from the operator reason catalog. The scoped fix is to document that
reason code in the source reason text and regenerate the checked-in catalog.

## Requirements Checklist

- [x] Confirm the reported focused pytest failure locally.
- [x] Add `MERGE_METHOD_MISMATCH` to the canonical doctor reason text with an
      actionable problem, likely cause, operator fix, related command, and docs
      link.
- [x] Regenerate `docs/REASON_CATALOG.md` from the canonical reason text so
      generated-doc drift checks stay intact.
- [x] Validate with the focused catalog coverage check and a focused catalog
      sync check, leaving full AWF/GitHub validation to the post-agent phase.
- [x] Commit the scoped fix locally without pushing.

## Implementation Steps

1. Reproduce `tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage`.
2. Inspect the merge-method mismatch emission path and reason catalog generator.
3. Add the missing reason text near related GitHub/PR monitor reason codes.
4. Run `scripts/generate_reason_catalog.py` to update the generated catalog.
5. Run focused pytest checks for catalog coverage and catalog generation drift.
6. Record validation evidence in `plans/PR353_REASON_CATALOG_CI_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q`
  - Passes with no missing reason codes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py::test_reason_catalog_file_matches_generated_content -q`
  - Passes with the checked-in catalog matching generated reason text.

Full AWF/GitHub validation is intentionally not run locally; AWF owns broad
validation after this agent phase.
