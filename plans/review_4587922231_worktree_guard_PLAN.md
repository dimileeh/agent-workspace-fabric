# Plan: Address review comment 4587922231 worktree guard cleanup

## Problem statement and scope
PR review comment `issue:4587922231` identified three small cleanup items in the validation worktree guard:

- executor ignored-snapshot baseline assignment happens before early-exit guards on an aborted initial pass;
- a pre-push helper name says "new ignored entries" even though it detects added, removed, or changed ignored entries;
- cleanup compares current ignored snapshot signatures through a raw path lookup while the baseline lookup is normalized.

Scope is limited to the affected executor/runtime modules, focused unit tests for the changed helper behavior, and the mandatory plan/validation documents.

## Requirements
- [ ] Move executor setup ignored-snapshot baseline assignment until after dirty-worktree and missing-HEAD early exits.
- [ ] Rename the pre-push ignored-entry helper and caller variable to describe drift, not only newly gained entries.
- [ ] Normalize current ignored signature lookup in validation worktree cleanup to match baseline lookup semantics.
- [ ] Add focused regression coverage for normalized current signature lookup.
- [ ] Run only targeted local checks for changed behavior; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation steps
1. Update `run_validation_and_fix_cycle` ordering so pre-existing dirty and missing HEAD exits run before the setup ignored baseline is assigned or compared.
2. Rename `_pre_push_validation_new_ignored_entries` to `_pre_push_validation_ignored_entries_drifted`, update the caller, and update focused tests that call the helper directly.
3. Reuse `_ignored_signature_lookup_by_normalized_path` for current ignored signatures in `cleanup_validation_worktree_side_effects`.
4. Add a unit test that simulates equivalent current/baseline signature paths with different trailing-slash formatting and verifies cleanup does not report modified ignored files.
5. Run targeted pytest selections for the touched unit tests.

## Verification commands
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_validation_worktree.py -k "ignored_signature or signature_lookup"`
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py -k "ignored_entries_drifted or ignored_entries"`
- `uv run --python 3.12 --extra dev pytest -q tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -k "missing_head or dirty_worktree or ignored_paths_after_initial_validation_pass or ignored_signature_drift"`
