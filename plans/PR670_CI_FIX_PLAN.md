# PR #670 CI failure repair plan

## Problem statement and scope

PR #670 is failing required checks in GitHub Actions. Two failures are code-related:

- `python-coverage-shards (6)` regression in `TestParseVerdict` because the latest
  `AWF-VERDICT: NEEDS_HUMAN:` placeholder was winning over an earlier
  concrete `NEEDS_HUMAN` reason.
- `python-coverage-shards (8)` (`tests/unit/test_core_decomposition_maintainability.py`)
  failed because `test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py` exceeds
  the 1500-line first-party file limit.

The transient `lint-and-type` package-fetch failure is not code-related.

Scope is strictly limited to `src/awf/runtime/pr_monitor_runner/helpers.py` and
`tests/unit/runtime/test_pr_monitor_runner_parts/`.

## Requirements checklist

- [x] Do not switch branches, push, rebase, or run broad AWF/GitHub validation.
- [x] Fix the `AWF-VERDICT` parsing behavior so a real `NEEDS_HUMAN` reason is
  preserved when followed by a placeholder verdict line in the same output.
- [x] Keep behavior minimal and preserve existing verdict priority ordering and parsing
  of prompt-echo fallback lines.
- [x] Resolve the file line-limit gate by splitting tests from the oversized shard-
  8 file into a new focused file.
- [x] Run focused regression tests for both touched test modules and the maintainability
  line-limit test.
- [x] Record evidence in `plans/PR670_CI_FIX_VALIDATION.md`.
- [x] Diagnose the latest `python-full-coverage` failure from run `28008500485`.
- [x] Add meaningful parser coverage for the uncovered verdict branches that caused
  the combined line+branch percentage to miss `99.00%` by `0.003%`.
- [x] Run focused parser tests with coverage against `helpers.py` and record evidence;
  leave full AWF/GitHub validation to AWF after this agent phase.

## Implementation steps

1. Update `_parse_verdict_result` in `helpers.py`:
   - When scanning matching verdicts, return immediately on the first parsed entry
     that has a concrete reason.
   - Keep placeholder-only results only if no concrete reason exists for that verdict.
2. Move `TestParseVerdict` from the oversized
   `test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py` into a new
   `test_pr_monitor_runner_part_017.py` so each test file remains under 1500 lines.
3. Remove now-unused imports from `test_pr_monitor_runner_part_004.py`.
4. Run focused commands for the changed test targets.

## Iteration 2: latest full-coverage near miss

GitHub Actions run `28008500485` reports:

- `python-full-coverage`: combined line+branch coverage `98.997%`, below required `99.00%`.
- `ci-required`: derivative failure because `python-full-coverage` failed.

The downloaded `coverage.xml` shows the PR-touched verdict parser still has uncovered
branches in `src/awf/runtime/pr_monitor_runner/helpers.py`:

- AWF canonical verdict with no usable reason and no earlier fallback reason returns
  the latest non-blocking verdict.
- Bare verdict priority selection preserves a no-reason verdict when no reasoned
  higher-priority verdict exists.

Focused implementation steps:

1. Add behavior tests to `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py`
   for placeholder-only canonical `FIXED` and bare `FALSE POSITIVE:` without a reason.
2. Run the parser test module and a targeted coverage command for `helpers.py`.
3. Update `plans/PR670_CI_FIX_VALIDATION.md` with the focused evidence and note that
   broad coverage validation remains AWF/GitHub-owned.
