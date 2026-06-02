# Address Review 4585090228 Plan

## Problem Statement And Scope

Review comment `issue:4585090228` reports that `src/awf/node/provisioner.py`
swallows the original `ComposeOperationError` when the compose-failure backstop
commit itself fails. The workspace state is still marked failed, but
`ControlWorker._safely_provision_claimed()` does not receive an exception and
therefore skips the `worker.provision_failed` log path.

Scope is limited to the provisioner compose-failure handler and focused
regression coverage for that double-failure path.

## Requirements Checklist

- Preserve the existing failed-workspace DB transition when the compose-failure
  backstop commit raises.
- Re-raise the original `ComposeOperationError` from the backstop-commit-failure
  path so worker-level failure logging and alerting still run.
- Do not mask the original compose exception with the secondary commit failure.
- Add or update a focused regression test for the double-failure path.
- Run only targeted validation for the changed behavior; full AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add a regression test that provisions an already-claimed workspace, forces
   stack launch to raise `ComposeOperationError`, and forces only the subsequent
   compose-failure backstop commit to raise.
2. Confirm the new test fails on the current implementation because the original
   compose exception is swallowed.
3. Update the `ComposeOperationError` handler to call `_mark_failed()` as before
   and then re-raise the original compose exception.
4. Re-run the targeted regression test and a nearby provisioner failure-path
   test to check no local behavior regressed.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py -q -k "compose_fail_backstop_commit_failure or stack_startup_failure_marks_workspace_failed_with_actionable_message"`

Pass criteria: targeted tests pass, and no broad AWF/GitHub-owned validation is
run inside the agent phase.
