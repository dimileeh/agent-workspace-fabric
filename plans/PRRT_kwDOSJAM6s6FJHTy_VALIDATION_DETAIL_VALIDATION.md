# PRRT_kwDOSJAM6s6FJHTy Validation Detail Validation

Plan reference: [PRRT_kwDOSJAM6s6FJHTy_VALIDATION_DETAIL_PLAN.md](./PRRT_kwDOSJAM6s6FJHTy_VALIDATION_DETAIL_PLAN.md)

## Requirement Status

1. Keep no-validation `detail` as the raw reason code, defaulting to `validation_unavailable`.
   - Complete
   - Evidence: `apps/console/lib/merge-queue-format.ts` now stores the raw no-validation `reasonCode` separately and assigns it to `detail`.
2. Preserve the current human-readable console `reasonLabel` behavior introduced for validation provenance clarity.
   - Complete
   - Evidence: `reasonLabel` continues to use `formatValidationReasonLabel(...)`, and focused formatter tests now assert human-readable label values.
3. Update focused formatter tests to assert the raw-detail and formatted-label split.
   - Complete
   - Evidence: `apps/console/lib/merge-queue-format.test.mjs` expects `detail: "validation_unavailable"` and `reasonLabel: "validation unavailable"` in the no-validation case.
4. Run only targeted console formatter checks.
   - Complete
   - Evidence: targeted commands below were run; full AWF/GitHub validation remains managed by AWF after agent completion.

## Verification

- Initial reproduction:
  - `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON apps/console/lib/merge-queue-format.test.mjs`
  - Failed before the fix because validation reason labels and no-validation detail were formatted inconsistently with the expected contract.
- Final focused regression:
  - `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON apps/console/lib/merge-queue-format.test.mjs`
  - Passed: 12 tests.
- Final focused lint:
  - `npm exec eslint -- lib/merge-queue-format.ts lib/merge-queue-format.test.mjs` from `apps/console`
  - Passed.

## Remaining Gaps

None for this review thread. Broad repository validation, frontend build, full coverage, push, and PR updates are intentionally left to AWF/GitHub.
