# Review 4491715538 Tool Diagnostics Plan

## Problem Statement and Scope

The review-level comment for `issue:4491715538` includes three quality-gate
diagnostic concerns. The unchanged PEP 735 dependency-group false positive is
already addressed in the current branch with a regression test and local commit,
so this iteration is scoped to the remaining actionable diagnostics:

- make raised `tool.coverage.report.fail_under` violations explain why the
  change is blocked without weakening the existing policy that any coverage
  policy change requires ownership;
- report every changed non-policy `tool.*` sub-section instead of stopping at
  the first one.

## Requirements Checklist

- Preserve the existing behavior that lowering and raising coverage
  `fail_under` values are blocked when `pyproject.toml` is unowned.
- Clarify the raised `fail_under` reason so it does not read like an allowed
  improvement.
- Add a regression test showing multiple changed unknown `tool.*` sections are
  all surfaced.
- Keep the production change focused in
  `src/awf/control/quality_gates.py`.
- Run the focused quality-gate unit tests that prove the fixes.
- Record validation evidence in
  `plans/REVIEW_4491715538_TOOL_DIAGNOSTICS_VALIDATION.md`.

## Implementation Steps

1. Update/add focused tests in `tests/unit/control/test_quality_gates.py` for
   raised `fail_under` explanation and multiple unknown tool-section
   diagnostics.
2. Run the new focused test selection and confirm it fails before production
   changes.
3. Update the raised `fail_under` reason text while preserving the violation.
4. Change unknown tool-section classification to collect and return all changed
   non-policy tool sections.
5. Re-run the focused tests plus the broader quality-gates unit file.
6. Write validation notes with requirement status and command evidence.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "raising_coverage_fail_under or unknown_tool"
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
```

Pass criteria: focused tests fail before implementation, pass after
implementation, and the full quality-gates unit file remains green.
