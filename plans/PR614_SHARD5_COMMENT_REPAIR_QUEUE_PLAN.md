# PR614 Shard 5 Comment Repair Queue Plan

## Problem Statement and Scope

CI run `27862959455` also fails in `python-coverage-shards (5)` at
`tests/unit/runtime/test_monitor_action_logging.py::TestMonitorDirtyWorktreeSalvage::test_comment_repair_gets_scope_correction_before_committing_protected_file`.

The focused local repro fails the same way: the test expects one review-thread
repair commit, but its `FakeCommandRunner` queue no longer matches the current
commit path. Protected-scope repair now verifies the repaired HEAD object before
running the repaired-status check, so the old queue shifts subsequent fake
results onto the wrong git commands.

Scope is limited to updating this test fixture queue and diagnostics to assert
the current behavior.

## Requirements Checklist

- Keep the current AWF-managed branch and do not push.
- Do not edit workflow, quality-gate, or protected configuration files.
- Preserve the test's behavior assertion: protected-scope correction happens
  before committing and exactly one review-thread fix commit is produced.
- Add only the missing fake command response needed by the current code path.
- Run the focused failing test after the change.
- Record that broad AWF/GitHub validation remains owned by AWF after completion.

## Implementation Steps

1. Insert the missing fake command result for post-repair HEAD verification in
   the failing test.
2. Keep or improve assertion diagnostics so future queue drift is visible.
3. Run the single failing test.
4. Re-run the previously fixed shard-8 focused checks if relevant after edits.
5. Commit this fix separately from the shard-8 line-limit repair.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py::TestMonitorDirtyWorktreeSalvage::test_comment_repair_gets_scope_correction_before_committing_protected_file -q`
  passes.
- Full sharded coverage and broad GitHub checks are not run locally; AWF/GitHub
  owns those gates after this agent phase.
