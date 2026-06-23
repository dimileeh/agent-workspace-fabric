# Honor Empty Final Needs Human Verdicts Plan

## Problem Statement and Scope

The PR monitor verdict parser must not let an earlier AWF verdict with a reason override a final AWF-prefixed `NEEDS_HUMAN` verdict whose reason is empty or sanitized away. That final blocking label must remain authoritative so unresolved review feedback is not incorrectly marked handled.

Scope is limited to verdict parsing behavior in `src/awf/runtime/pr_monitor_runner/helpers.py` and focused parser regression tests.

## Requirements Checklist

- Preserve the final AWF-prefixed `NEEDS_HUMAN` verdict even when its reason is empty or a template placeholder.
- Preserve existing safety behavior for sanitized final non-blocking AWF verdicts that would otherwise override a prior blocking verdict.
- Add focused regression coverage for the review-thread scenario.
- Run only targeted tests for the touched parser behavior.
- Do not run broad AWF or CI-equivalent validation; AWF/GitHub owns broad validation after agent completion.

## Implementation Steps

1. Add parser regression tests for `FIXED` followed by empty and placeholder final `NEEDS_HUMAN`.
2. Adjust AWF-prefixed no-reason verdict fallback logic so blocking final labels remain authoritative, while sanitized final non-blocking labels can fall back to the prior reasoned verdict.
3. Run the focused parser test file.
4. Record validation evidence in `plans/HONOR_EMPTY_FINAL_NEEDS_HUMAN_VERDICTS_VALIDATION.md`.
