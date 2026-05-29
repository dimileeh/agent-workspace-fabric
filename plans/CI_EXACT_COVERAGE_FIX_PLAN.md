# CI Exact Coverage Fix Plan

## Problem Statement And Scope

PR #296 fails the GitHub Actions `python-full-coverage` exact threshold step.
The full test command passed, but `scripts/ci/check_coverage_threshold.py`
reported combined line+branch coverage of 98.9999%, just below the required
99.00%. The fix must add meaningful coverage without weakening CI, changing
protected workflow configuration, pushing, rebasing, or running broad AWF-owned
validation locally.

## Requirements Checklist

- [ ] Preserve the current AWF-managed branch and do not push.
- [ ] Do not edit protected workflow, quality-gate, or configuration files.
- [ ] Add focused regression coverage that exercises real behavior related to
      the PR's CLI bootstrap/init migration surface.
- [ ] Run focused local checks only; leave full coverage/CI validation to AWF
      and GitHub after agent completion.
- [ ] Record validation evidence in a matching validation document.
- [ ] Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Use the CI coverage artifact to identify a concrete uncovered branch.
2. Add a targeted unit test for the preserved init bootstrap helper's invalid
   provider validation path.
3. Run the focused CLI test file, plus a focused non-gating coverage probe for
   `awf.cli.init_ops` to confirm the previously uncovered branch is exercised.
4. Create `plans/CI_EXACT_COVERAGE_FIX_VALIDATION.md` with requirement status
   and evidence.
5. Commit the plan, test, and validation documents locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_002.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_002.py --cov=awf.cli.init_ops --cov-report=term-missing --cov-fail-under=0 -q`
  must pass and show the invalid-provider branch is covered by the focused
  test file.
- Full AWF/GitHub broad validation, including the exact repository-wide
  coverage gate, is intentionally not run locally in this agent phase.

## Assumptions/Changes

- The repository's global coverage fail-under applies even to a deliberately
  narrow `--cov=awf.cli.init_ops` probe. The focused probe therefore sets
  `--cov-fail-under=0` so it remains evidence gathering rather than a local
  substitute for AWF/GitHub's broad exact coverage gate.
