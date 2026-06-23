# Plan: Handle AWF-VERDICT empty-reason outputs as authoritative blocking verdicts

## Problem
The review feedback parser currently requires at least one character in the `AWF-VERDICT` reason capture. `AWF-VERDICT: NEEDS_HUMAN:` or `AWF-VERDICT: DEFER:` therefore do not match and can fall through to `fix_committed`, which can incorrectly unblock review threads.

## Scope
- Keep the fix narrow: only behavior in verdict parsing for `AWF-VERDICT` lines.
- Preserve existing normalization and sanitization behavior for non-empty reasons.
- Add regression coverage for empty-reason `AWF-VERDICT` labels.

## Requirements checklist
- [ ] Allow `AWF-VERDICT` to match when reason is empty.
- [ ] Preserve existing verdict label precedence and normalization logic.
- [ ] Add tests proving `AWF-VERDICT: NEEDS_HUMAN:` and `AWF-VERDICT: DEFER:` map to blocking/deferral verdicts.
- [ ] Keep edits minimal and avoid touching unrelated parsing logic.

## Implementation steps
1. Update `src/awf/runtime/pr_monitor_runner/constants.py`:
   - Change `_AWF_VERDICT` reason capture from `+` to `*`.
2. Update `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py`:
   - Add focused regression tests for empty reason variants.

## Verification
- Suggested focused checks (no full AWF/GitHub suite):
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py -k "private_awf_verdict" -q`
- Acceptance criteria:
  - Empty-reason `AWF-VERDICT: NEEDS_HUMAN:` and `AWF-VERDICT: DEFER:` parse to `needs_human` and `defer` respectively.
  - Existing non-empty labeled verdict expectations remain unchanged.
