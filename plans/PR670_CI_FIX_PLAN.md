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

- [ ] Do not switch branches, push, rebase, or run broad AWF/GitHub validation.
- [ ] Fix the `AWF-VERDICT` parsing behavior so a real `NEEDS_HUMAN` reason is
  preserved when followed by a placeholder verdict line in the same output.
- [ ] Keep behavior minimal and preserve existing verdict priority ordering and parsing
  of prompt-echo fallback lines.
- [ ] Resolve the file line-limit gate by splitting tests from the oversized shard-
  8 file into a new focused file.
- [ ] Run focused regression tests for both touched test modules and the maintainability
  line-limit test.
- [ ] Record evidence in `plans/PR670_CI_FIX_VALIDATION.md`.

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
