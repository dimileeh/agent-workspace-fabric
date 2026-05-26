# File Decomposition Plan - Finish AWF Core Monolith Split

## Summary

Finish the maintainability refactor already started in the current uncommitted
tree. The repository layer split is accepted as the model. The remaining work is
to finish decomposing:

- `src/awf/control/executor/base.py`
- `src/awf/runtime/pr_monitor_runner/runner.py`
- `src/awf/control/worker/manager.py`

This is a no-behavior-change refactor for open-source readiness. The goal is to
leave each main orchestrator file with only core orchestration, state
transitions, and explicit typed delegation methods. Domain logic moves into
focused sibling modules. Do not add dynamic patch proxies back.

## Key Changes

- Normalize the current partial split first:
  - Keep package-root imports working through `__init__.py` re-exports.
  - Keep tests patching implementation submodules directly, not package-root
    dynamic proxies.
  - Remove the remaining worker dynamic proxy block from
    `worker/manager.py`.
- Finish PR monitor runner decomposition:
  - Keep `runner.py` focused on `run`, `_execute`, state load/persist,
    terminal transitions, compose teardown, and workspace filesystem GC.
  - Move comment fix-cycle, feedback resolution, merge-gate event logging,
    protected-scope repair, dirty-worktree commit, and remote branch diff logic
    into focused sibling modules.
- Finish worker decomposition:
  - Keep `manager.py` focused on `run_forever`, `run_once`, dispatch loops,
    task bookkeeping, and explicit delegates.
  - Move stale-active/preserved-active recovery into `recovery.py`.
  - Move terminal runtime and secret-lease cleanup into `cleanup.py`.
  - Move claim/capacity methods into `claims.py`.
  - Move scheduler/provider-recovery queue filtering delegates into
    `scheduler_methods.py`.
- Finish executor decomposition:
  - Keep `base.py` focused on `WorkspaceExecutor.__init__`, `execute`,
    `resume_pr_monitor`, workspace load/claim/transition/subphase methods, and
    explicit delegates.
  - Move PR handoff/sync helpers into `monitor_handoff.py`.
  - Move planning and conformance loop helpers into `planning_ops.py`.
  - Move git recovery/diff/rebase helpers into `git_methods.py`.
  - Move post-agent quality-gate repair helpers into `quality_methods.py`.
  - Move validation callback/run bookkeeping into `validation_ops.py`.

## Delegation Rules

- Move code mechanically first; avoid rewriting logic.
- Preserve method signatures where practical.
- Delegates may pass `self` into module-level functions when that avoids risky
  state reshaping.
- Do not use blanket `*args, **kwargs` delegates for normal methods.
- Do not add dynamic patch proxies back.
- Do not introduce new abstractions, service classes, or behavior changes just
  to make the split look cleaner.

## Acceptance Criteria

- The DB repository split remains intact and import-compatible.
- No dynamic proxy wrappers remain in `executor/base.py`, `runner.py`, or
  `worker/manager.py`.
- Each remaining main orchestrator file should target `<1,500` lines. If
  forcing that target would require behavior rewrites, stop at the smallest safe
  residual size and document the remaining extraction in validation.
- `plans/FILE_DECOMPOSITION_VALIDATION.md` describes what actually changed. Do
  not claim full decomposition or full-suite success unless verified.
- Public imports continue to work:
  - `from awf.control.executor import WorkspaceExecutor`
  - `from awf.control.worker import ControlWorker`
  - `from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner`
  - `from awf.db.repositories import WorkspaceRepository`

## Test Plan

Run focused tests after each subsystem extraction:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_runner.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py \
  tests/unit/runtime/test_monitor_completion_gc.py \
  -q

uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_worker.py \
  tests/unit/control/test_worker_coverage_edges.py \
  -q

uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor.py \
  tests/unit/control/test_executor_coverage_edges.py \
  tests/unit/control/test_executor_monitor_recovery.py \
  -q
```

Final validation:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
uv run --python 3.12 --extra dev pytest tests/unit -q
```

If full `tests/unit` is too slow for the iteration, record the exact focused
suites run and leave full unit coverage as a required pre-merge gate.

## Assumptions

- This plan starts from the current uncommitted tree, where repository
  decomposition is already present and executor/runner/worker decomposition is
  partial.
- No AWF runtime behavior, scheduling policy, merge policy, provider recovery,
  validation semantics, or API behavior should change.
- Test-only patch target changes are allowed when they reflect the new module
  boundaries.
- Generated `__pycache__` files are ignored and must not be tracked.
