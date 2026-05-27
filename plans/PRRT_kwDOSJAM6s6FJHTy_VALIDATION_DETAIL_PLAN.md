# PRRT_kwDOSJAM6s6FJHTy Validation Detail Plan

## Problem Statement And Scope

Address PR review thread `PRRT_kwDOSJAM6s6FJHTy` for the console merge-queue validation formatter. The no-validation path currently assigns the formatted `reasonLabel` to `detail`, so callers that expect `detail` to carry the raw validation reason code receive human-facing text such as `validation unavailable`.

## Requirements

- [x] Keep no-validation `detail` as the raw reason code, defaulting to `validation_unavailable`.
- [x] Preserve the current human-readable console `reasonLabel` behavior introduced for validation provenance clarity.
- [x] Update focused formatter tests to assert the raw-detail and formatted-label split.
- [x] Run only the targeted console formatter test file; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Split the no-validation formatter branch into a raw `reasonCode` and formatted `reasonLabel`.
2. Set no-validation `detail` from the raw `reasonCode`.
3. Update `apps/console/lib/merge-queue-format.test.mjs` expectations for human-facing reason labels while preserving raw details.
4. Run `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON apps/console/lib/merge-queue-format.test.mjs`.

## Pass Criteria

- The focused console formatter test file passes.
- The no-validation path returns raw `detail` and formatted `reasonLabel`.
- No broad repository validation, full frontend build, full coverage run, push, rebase, or branch switch is performed.
