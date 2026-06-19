# PR608 Coverage Margin Fix Plan

## Problem Statement and Scope

PR #608 has a recent failed CI run where `python-full-coverage` combined all
coverage shards successfully but reported total coverage of `98.97`, below the
required `99.00`. The dependent `ci-required` job failed only because
`python-full-coverage` failed. A newer CI run is in progress for the current
HEAD, but the failed run's uploaded `coverage.xml` shows uncovered behavior in
the post-validation planning conformance artifact fallback code touched by this
PR.

Scope is limited to focused tests or minimal production fixes for the uncovered
planning conformance behavior. Do not edit workflow, quality gate, or protected
configuration files. Do not run full-repository coverage locally; AWF/GitHub CI
owns the broad gate after this agent phase.

## Requirements Checklist

- Inspect PR #608 Actions logs and coverage artifact before changing code.
- Add behavior-focused coverage for real planning conformance artifact fallback
  behavior, not hollow line execution.
- Keep changes scoped to tests and plan/validation documentation unless a real
  production bug is found.
- Run targeted tests only for the changed behavior.
- Commit the local fix with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Use the failed run's `full-coverage-report` artifact to identify uncovered
   conformance lines and confirm the current run status.
2. Add focused tests around `_deposit_satisfied_conformance_report` error and
   stale-artifact cleanup behavior, because this code decides whether served
   conformance artifacts match the recorded post-validation event when the
   worktree report cannot be rewritten.
3. Run the narrow affected test file(s), optionally with a targeted coverage
   report for `planning_conformance.py` only.
4. Check current CI status again and document any remaining pending checks as
   AWF/GitHub-owned validation.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused test file> -q`
  - Passes.
- Optional targeted coverage command limited to changed conformance tests/module.
  - Shows the new behavior executes the previously missed fallback paths.
- `gh pr checks 608 --watch=false`
  - Used for status inspection only; full CI completion remains AWF/GitHub-owned.
