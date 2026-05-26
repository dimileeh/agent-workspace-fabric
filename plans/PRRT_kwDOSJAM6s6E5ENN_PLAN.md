# PRRT_kwDOSJAM6s6E5ENN Plan

## Problem Statement and Scope

The PR monitor rebase-recovery path rebases again when local `HEAD` already
contains `origin/<base>` but `origin/<remote_branch>` does not. That restart
case means the local branch is already refreshed and the remote PR branch is
lagging, so recovery should preserve the existing local head and let the normal
existing-PR update path push it after focused validation.

Scope is limited to the executor rebase-recovery branch and focused regression
coverage for this lagging-remote case.

## Requirements Checklist

- Detect `HEAD` already containing `origin/<base>` while the remote PR branch
  does not.
- Avoid running `git rebase` in that lagging-remote case.
- Record the current recovery head with `rebased=false` and `pushed=false`.
- Ensure the caller still updates the existing PR branch after validation when
  rebase recovery recorded a head that was not pushed.
- Preserve the already-synced remote case where no push is needed.

## Implementation Steps

1. Update the focused regression around the lagging-remote case so it expects no
   rebase and a later existing-PR push.
2. Add the minimal state needed for the caller to know whether rebase recovery
   already pushed the recorded head.
3. Update `_run_monitor_rebase_recovery` to record and return the current head
   when only the remote is lagging.
4. Update the existing-PR push decision for rebase-only recovery to push when
   the recorded rebase-recovery head was not pushed.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_003.py -q`
  must pass.
- Broad AWF/GitHub validation remains owned by AWF after agent completion.
