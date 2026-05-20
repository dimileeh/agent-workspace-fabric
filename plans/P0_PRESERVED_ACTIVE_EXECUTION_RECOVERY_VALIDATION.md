# P0 Preserved Active Execution Recovery Validation

Plan reference: `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_PLAN.md`

Implementation contract reference: `docs/awf-plans/ws_77bb4cce4aea4892bb41e0e6.md`

## Requirement Status

- Complete: Treat `ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART` as recovery start.
  - Evidence: `src/awf/control/worker.py` now invokes preserved-active recovery after recording preservation and on later scans.
- Complete: Recover existing PRs by attaching one PR monitor.
  - Evidence: `test_preserved_active_pr_handoff_attaches_one_monitor_after_restart`.
- Complete: Recover clean committed work through validation continuation.
  - Evidence: `test_preserved_active_clean_committed_work_dispatches_validation_salvage_once`; `src/awf/control/executor.py` accepts `worker_restart` validate-only recovery.
- Complete: Create one lineage-preserving replacement when no usable work exists.
  - Evidence: `test_preserved_active_without_usable_work_creates_one_replacement_with_lineage`.
- Complete: Leave ambiguous cases operator-recoverable without failed runtime release.
  - Evidence: `test_preserved_active_ambiguous_dirty_worktree_is_operator_recoverable`.
- Complete: Preserve stale-active failure for true orphan/no-recovery cases.
  - Evidence: `test_stale_active_failure_still_applies_after_salvage_not_possible_for_orphan`.
- Complete: Preserve reason codes, events, operations, and task/attempt lineage.
  - Evidence: new salvage reason codes/events and operation payload assertions in `tests/unit/control/test_worker.py`.
- Complete: Keep salvage idempotent across repeated scans.
  - Evidence: repeated `worker.run_once()` assertions in the new regression tests.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q
# 191 passed in 215.17s

uv run --python 3.12 --extra dev pytest tests/unit/control tests/unit/service -q
# 2564 passed in 1122.24s

uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control tests/unit/service
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 157 source files
```

## Files Changed

- `src/awf/control/worker.py`
- `src/awf/control/executor.py`
- `tests/unit/control/test_worker.py`
- `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_PLAN.md`
- `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_VALIDATION.md`

No known gaps remain for this plan slice.

## Iteration 1: Pushed Branch Open-PR Salvage

Plan update: `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_PLAN.md`

### Requirement Status

- Complete: Preserved active workspaces with no persisted `pr_url`/`pr_number`
  now try `remote_push_branch` or `branch_name` before falling back to worktree
  validation/replacement.
  - Evidence: `src/awf/control/worker.py` adds an injectable branch open-PR
    resolver and invokes it before worktree classification.
- Complete: Exactly one matching open PR is adopted by persisting `pr_url`,
  `pr_number`, `remote_push_branch`, and head SHA metadata, then attaching one
  PR monitor with duplicate-monitor protection.
  - Evidence:
    `test_preserved_active_pushed_branch_open_pr_attaches_one_monitor_after_restart`
    and `test_preserved_active_pushed_branch_lookup_falls_back_to_branch_name`.
- Complete: Lookup failures and multiple matching open PRs become explicit
  operator-recoverable ambiguity and do not release/fail the runtime.
  - Evidence:
    `test_preserved_active_pushed_branch_pr_lookup_ambiguity_is_operator_recoverable`.
- Complete: Production worker wiring provides the GitHub-backed branch resolver.
  - Evidence: `src/awf/common/github_client.py` adds
    `BranchOpenPullRequestResolver`; `src/awf/service/worker.py` wires it into
    `ControlWorker`; `tests/unit/service/test_worker.py` asserts the wiring.

### TDD Evidence

The new focused tests were added before implementation and first failed because
`ControlWorker` did not yet accept `open_pr_resolver`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "pushed_branch_open_pr or pushed_branch_lookup_falls_back or pushed_branch_pr_lookup_ambiguity"
# 4 failed, 191 deselected
```

### Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "pushed_branch_open_pr or pushed_branch_lookup_falls_back or pushed_branch_pr_lookup_ambiguity"
# 4 passed, 191 deselected in 9.92s

uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q
# 195 passed in 319.49s

uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py::test_build_worker_runtime_wires_executor_and_feature_monitor_factory tests/unit/service/test_worker.py::test_build_worker_runtime_uses_local_service_node_id_instead_of_container_hostname -q
# 2 passed in 1.16s

uv run --python 3.12 --extra dev pytest tests/unit/control tests/unit/service -q
# 2568 passed in 1347.91s

uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control tests/unit/service
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 157 source files
```

### Files Changed In Iteration 1

- `src/awf/common/github_client.py`
- `src/awf/control/worker.py`
- `src/awf/service/worker.py`
- `tests/unit/control/test_worker.py`
- `tests/unit/service/test_worker.py`
- `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_PLAN.md`
- `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_VALIDATION.md`

No known gaps remain for the pushed-branch/open-PR salvage path.
