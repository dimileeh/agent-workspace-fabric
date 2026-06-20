# PR614 Full Coverage 2026-06-20 Plan

## Problem Statement And Scope

PR #614 fails CI because `python-full-coverage` combines eight successful
coverage shards and reports 98.93% coverage against the required 99.00%.
`ci-required` fails only because `python-full-coverage` fails.

Scope is limited to adding meaningful focused regression coverage for behavior
already introduced on this PR. Do not edit CI workflow or quality gate
configuration, and do not run full repository coverage locally; AWF/GitHub own
the broad gate after this agent phase.

## Requirements Checklist

- Diagnose the failing CI check from Actions evidence and coverage artifact.
- Add or update focused tests that assert real behavior, not coverage padding.
- Keep changes scoped to tests/plan/validation unless diagnosis finds a product
  bug that requires source changes.
- Run focused verification for the touched test file(s) only.
- Record validation evidence and note that full coverage validation is deferred
  to AWF/GitHub.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Use the downloaded CI `coverage.xml` to choose uncovered PR-touched behavior.
2. Add focused tests for `src/awf/control/executor/helpers.py` uncovered
   branches around validation command record construction and failure messages.
3. Run the targeted pytest selection for the edited test file.
4. Run focused lint on the edited test file.
5. Create `plans/PR614_FULL_COVERAGE_20260620_VALIDATION.md`.
6. Commit the plan, tests, and validation locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest <edited-test-file> -q`
  - Passes all tests in the edited file.
- `uv run --python 3.12 --extra dev ruff check <edited-test-file>`
  - Reports no lint issues.

Full `python-full-coverage`, full unit suite, and CI-required validation are
intentionally not run locally in this AWF agent phase; AWF/GitHub perform those
broad gates after agent completion.
