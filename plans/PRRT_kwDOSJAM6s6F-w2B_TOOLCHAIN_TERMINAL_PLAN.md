# PRRT_kwDOSJAM6s6F-w2B Toolchain-Missing Terminal Failure Plan

## Goal

Resolve review thread `PRRT_kwDOSJAM6s6F-w2B` by making pure pre-push
validation toolchain-missing failures terminal for the PR monitor, so the
monitor fails fast with the specific reason instead of retrying until the
decision loop limit.

## Scope

- Update focused regression coverage for `_GitPushResult.terminal_monitor_failure`.
- Update `src/awf/runtime/pr_monitor_runner/remote_ops.py` only as needed to
  include `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` in the terminal failure set.
- Do not change broad validation, branch management, pushing, or GitHub thread
  state; AWF owns those after this agent phase.

## Steps

1. Add a failing unit regression that asserts a failed push result with
   `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` is terminal.
2. Run the focused regression and confirm it fails for the missing terminal
   classification.
3. Add the shared `_PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON` constant to
   `_GitPushResult.terminal_monitor_failure`.
4. Re-run the focused remote-ops unit test file.
5. Record validation evidence and commit the scoped changes locally.
