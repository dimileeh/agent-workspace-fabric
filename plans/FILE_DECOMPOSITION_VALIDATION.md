# File Decomposition Validation

Plan reference: `plans/FILE_DECOMPOSITION_PLAN.md`

## What Changed

- Preserved the accepted DB repository split and package facade:
  - `from awf.db.repositories import WorkspaceRepository` still works.
- Finished the no-behavior-change split for the active AWF core packages:
  - `src/awf/control/executor/`
  - `src/awf/runtime/pr_monitor_runner/`
  - `src/awf/control/worker/`
- Kept public compatibility imports stable:
  - `from awf.control.executor import WorkspaceExecutor`
  - `from awf.control.worker import ControlWorker`
  - `from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner`
  - `from awf.db.repositories import WorkspaceRepository`
- Removed the temporary hydration/proxy patterns from the target packages:
  - no `_hydrate` helpers;
  - no `globals()` namespace copying;
  - no `__dict__.update(...)` namespace copying;
  - no package `__file__` reassignment hack;
  - no file-level `# mypy: ignore-errors`;
  - no broad `ruff: noqa: E402, F401, F821` import masking.
- Added a maintainability guard test at
  `tests/unit/test_core_decomposition_maintainability.py`.
- Updated tests to patch implementation submodules directly instead of using
  package-level dynamic proxy behavior.
- Finished the boundary cleanup review pass:
  - `awf.control.executor` now exports only `ExecutorConfig` and
    `WorkspaceExecutor`;
  - `awf.control.worker` now exports only `ControlWorker`, `WorkerConfig`, and
    `SCHEDULER_SQL_AGE_BOOST_DIALECTS`;
  - `awf.runtime.pr_monitor_runner` now exports only `MonitorRunnerConfig`,
    `PostMergeTargetReconciler`, and `PullRequestMonitorRunner`;
  - package facades no longer use dynamic `__getattr__`, `import_module`, or
    compatibility module scans;
  - implementation modules no longer import from orchestrator barrels
    (`executor/base.py`, `pr_monitor_runner/runner.py`, or
    `worker/manager.py`);
  - `base.py`, `runner.py`, and `manager.py` no longer expose broad private
    `__all__` barrels.

## Final Core Line Counts

All files in the target core packages are below the 1,500-line threshold.

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`: 1,435 lines
- `src/awf/control/executor/execution_flow.py`: 1,393 lines
- `src/awf/runtime/pr_monitor_runner/helpers.py`: 1,324 lines
- `src/awf/control/worker/recovery_preserved_flow.py`: 1,314 lines
- `src/awf/runtime/pr_monitor_runner/loop.py`: 1,175 lines
- `src/awf/control/executor/quality_methods.py`: 1,168 lines
- `src/awf/control/executor/git_methods.py`: 1,084 lines
- `src/awf/control/executor/planning_ops.py`: 1,068 lines
- `src/awf/control/executor/execution_validation.py`: 1,029 lines
- `src/awf/control/executor/helpers.py`: 1,005 lines
- `src/awf/control/executor/monitor_handoff.py`: 968 lines
- `src/awf/control/worker/recovery_stale.py`: 936 lines
- `src/awf/control/worker/helpers.py`: 877 lines
- `src/awf/control/worker/recovery_preserved_queries.py`: 810 lines
- `src/awf/runtime/pr_monitor_runner/gates.py`: 780 lines
- `src/awf/control/executor/shared.py`: 751 lines
- `src/awf/runtime/pr_monitor_runner/remote_ops.py`: 699 lines
- `src/awf/control/worker/claims.py`: 660 lines
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`: 640 lines
- `src/awf/control/worker/shared.py`: 613 lines
- `src/awf/runtime/pr_monitor_runner/shared.py`: 587 lines
- `src/awf/control/worker/manager.py`: 549 lines
- `src/awf/runtime/pr_monitor_runner/runner.py`: 497 lines
- `src/awf/control/executor/base.py`: 195 lines

## Validation

Passed:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Result: `All checks passed!`

Passed:

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 220 source files`

Passed:

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```

Result: `OK: openapi.json matches the current app spec.`

Passed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
```

Result: `8 passed`

Passed:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor.py \
  tests/unit/control/test_executor_coverage_edges.py \
  tests/unit/control/test_executor_monitor_recovery.py \
  tests/unit/control/test_executor_error_paths.py \
  tests/unit/runtime/test_pr_monitor_runner.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py \
  tests/unit/runtime/test_monitor_completion_gc.py \
  tests/unit/control/test_worker.py \
  tests/unit/control/test_worker_coverage_edges.py \
  tests/unit/test_core_decomposition_maintainability.py \
  -q -n 20
```

Result: `1154 passed in 302.26s (0:05:02)`

Passed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit -q -n 20
```

Result: `7785 passed in 1964.00s (0:32:43)`

## Follow-Up OSS Maintainability Backlog

The current iteration intentionally scoped enforcement to the decomposed core
packages. The next OSS maintainability pass should target remaining large
files outside this split, including:

- `src/awf/cli/main.py`
- `src/awf/service/workspaces.py`
- `src/awf/control/quality_gates.py`
- `src/awf/runtime/validation.py`

Those files should get their own plan and tests rather than being mixed into
this already-large no-behavior-change refactor.
