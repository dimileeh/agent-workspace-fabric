# PR259 Review Log Reversal Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6CjUpo` flags `apps/console/lib/format.ts`
for reversing log chunks with `split("\n").reverse().join("\n")`, which can
materialize unnecessary intermediate arrays and strings for large log frames.

Scope is limited to the console log formatter and its regression coverage.

## Requirements Checklist

- Preserve existing ascending and descending log rendering behavior.
- Avoid using `String.prototype.split("\n").reverse().join("\n")` for descending
  chunk line reversal.
- Add a regression test that catches reintroducing split-based log reversal.
- Validate with the narrow console formatter test.

## Implementation Steps

1. Add a failing regression test in `apps/console/lib/format.test.mjs`.
2. Replace split/reverse/join with a backward newline scan in
   `apps/console/lib/format.ts`.
3. Run the focused console formatter test.
4. Record validation evidence in `plans/PR259_REVIEW_LOG_REVERSAL_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `npm --prefix apps/console run test -- lib/format.test.mjs`
  - Passes with the new regression and existing formatter coverage.
