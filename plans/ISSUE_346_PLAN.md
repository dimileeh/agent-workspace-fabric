# Issue 346 Plan

## Problem Statement and Scope

PR-monitor pre-push validation currently runs profile `post_agent` and
`validate` commands without ensuring the profile `setup` phase has prepared the
monitor workspace toolchain. When commands such as `ruff check .` or
`npm run lint` are missing, the shell returns `127`; AWF treats that as an
ordinary validation failure, spends the comment-repair fix-pass budget, and
surfaces `PRE_PUSH_VALIDATION_FIX_FAILED`.

This change will make the monitor path reuse the existing validation setup
machinery, classify pure command-not-found validation failures distinctly, and
include failing command diagnostics in persisted pre-push failure details.

## Requirements Checklist

- [ ] Reuse existing `ValidationRunner` setup-phase behavior for monitor
      pre-push validation setup; do not add project-specific installers.
- [ ] Avoid running setup on every pre-push validation cycle when a one-time
      monitor provision/adoption seam can provide the toolchain.
- [ ] Classify pure `returncode == 127` pre-push validation failures as
      `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING`.
- [ ] Do not consume pre-push validation fix-pass budget for pure toolchain
      missing failures.
- [ ] Preserve current fix-pass behavior for genuine validation failures such
      as lint/typecheck exit code `1`.
- [ ] Prefer genuine validation failures when mixed with `127` failures.
- [ ] Include the failing command and return code in pre-push failure details,
      using existing redaction helpers for command text.
- [ ] Add focused regression tests for classification, fix-pass behavior,
      failure payload diagnostics, precedence, and setup reuse.
- [ ] Keep changes scoped to AWF generic runtime/control-plane behavior.

## Implementation Steps

1. Inspect the monitor adoption/provision/recovery paths to find where setup
   belongs.
2. Add failing tests in `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
   and the focused executor/monitor handoff test file for the selected setup
   seam.
3. Add `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` to pre-push validation constants
   and reason propagation.
4. Add small helpers in pre-push validation to identify failed commands, pure
   `127` failures, mixed failures, and redacted command diagnostics.
5. Bypass `_run_pre_push_validation_fix_pass()` for pure toolchain-missing
   validation results.
6. Enrich `_PrePushValidationResult.failure_details()` with failing command and
   return code where available.
7. Apply the setup fix at the verified one-time monitor setup seam, using
   `ValidationRunner.run_profile_phases()` and existing setup phase ordering.
8. Validate with targeted tests and static checks, then record results in
   `plans/ISSUE_346_VALIDATION.md`.

## Assumptions/Changes

- Investigation found the normal feature workspace executor path already runs
  `setup`/`pre_agent`; the missing setup seam is the sync PR monitor handoff
  path (`sync_feature_pr` and `sync_release_pr`) that transitions directly to
  `monitoring_pr`.
- The setup implementation lives in a small `monitor_handoff_setup` helper to
  keep `monitor_handoff.py` below the repository line-count guard.
- Full `pytest` was run because the task explicitly requested the broad local
  gate. The repeated failures after this change were outside the touched issue
  #346 surface and passed when rerun directly; they are documented in the
  validation record.

## Verification Commands and Pass Criteria

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q
uv run --python 3.12 --extra dev pytest <focused setup seam test file> -q
uv run --python 3.12 --extra dev ruff check <touched source and test files>
uv run --python 3.12 --extra dev mypy <touched source files>
```

The task request explicitly asks for the full local gate, so after focused
checks pass, run the requested broad Python checks if time and environment
permit. AWF/GitHub still own post-agent validation provenance, merge gating,
and PR lifecycle.
