# PR614 shard 3 executor helper binding validation

Plan reference: `plans/PR614_SHARD3_EXECUTOR_HELPER_BINDING_PLAN.md`

## Requirement status

- Complete: Preserved AWF branch ownership. No branch switch, push, rebase, or
  broad AWF/GitHub-owned validation was run.
- Complete: Reproduced the shard-3 failure locally with the focused CI target
  before editing. The failure matched CI: `UnboundLocalError` for
  `_repair_mirror_hooks_path_or_mark_failed` in `execution_flow.py`.
- Complete: Kept the code change scoped to the executor mirror-hooks helper
  binding path by moving the existing local helper bindings before adapter
  initialization.
- Complete: Preserved fail-closed mirror repair behavior for setup/agent
  failure paths. The same helper implementation is now available to both
  normal and adapter-initialization recovery paths.
- Complete: Ran focused tests for the failing target and nearby executor
  mirror-hooks coverage.
- Complete: Ran focused lint for touched files.

## Evidence

Files changed:

- `src/awf/control/executor/execution_flow.py`
- `plans/PR614_SHARD3_EXECUTOR_HELPER_BINDING_PLAN.md`
- `plans/PR614_SHARD3_EXECUTOR_HELPER_BINDING_VALIDATION.md`

Commands run:

- `gh run list --commit HEAD --limit 20`
  - Result: no runs returned for the local commit context.
- `gh pr checks 614 --json name,state,bucket,link,startedAt,completedAt,workflow`
  - Result: latest current run still in progress; previous completed failure was
    `python-coverage-shards (3)`.
- `gh run view 27846850388 --repo dimileeh/agent-workspace-fabric --json name,workflowName,conclusion,status,url,event,headBranch,headSha,jobs`
  - Result: failed run identified `python-coverage-shards (3)` as the
    actionable failure. `python-full-coverage` and `ci-required` were downstream.
- `gh run view 27846850388 --repo dimileeh/agent-workspace-fabric --log-failed`
  - Result: failure snippet showed
    `tests/unit/control/test_executor_parts/test_executor_part_006.py::TestAdapterInitFailure::test_missing_head_before_adapter_init_marks_failed_when_adapter_none`
    failing with `UnboundLocalError` at `src/awf/control/executor/execution_flow.py`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_006.py::TestAdapterInitFailure::test_missing_head_before_adapter_init_marks_failed_when_adapter_none -q`
  - Before: `1 failed`.
  - After: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Result: `8 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py plans/PR614_SHARD3_EXECUTOR_HELPER_BINDING_PLAN.md`
  - Result: passed.

## Residual risk

Full AWF/GitHub validation, the full coverage merge, and CI-equivalent gates
were intentionally not run locally. AWF owns those after this agent phase.
