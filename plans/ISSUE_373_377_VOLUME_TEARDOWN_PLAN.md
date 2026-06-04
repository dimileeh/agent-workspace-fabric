# Issue #373/#377 Volume Teardown Plan

## Problem Statement and Scope

Implement the saved AWF plan from `docs/awf-plans/ws_9c0778a441bd42eaac10170c.md`.
Close the per-workspace Docker volume teardown gaps for issue #373 and issue #377.

In scope:

- `src/awf/node/compose_manager.py`
- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- Focused unit tests for those behaviors

Out of scope:

- `src/awf/service/gc.py`
- `src/awf/node/auth_mounts.py`
- GC deletion routing, broad refactors, frontend, migrations, OpenAPI

## Requirements Checklist

- Missing compose files in `ComposeManager.teardown_project` fall back to label-scoped teardown.
- The genuine no-resource missing-compose case stays idempotent and successful.
- Label fallback failures preserve specific `ComposeOperationError.reason_code` values.
- Post-merge monitor filesystem GC passes a volume-removing `compose_teardown` callback into `run_workspace_filesystem_gc`.
- Post-merge compose teardown failures gate filesystem deletion through the GC engine result.
- Tests are written or updated before implementation changes where behavior is not already covered.
- Validation uses focused checks; broad AWF/GitHub validation and full repository coverage remain post-agent responsibilities.

## Implementation Steps

1. Confirm existing compose-manager implementation/tests against the #373 contract.
2. Add lifecycle tests for post-merge compose callback wiring and teardown-failure gating.
3. Implement a lifecycle-local callback backed by `ComposeManager.teardown_project(remove_volumes=True)`.
4. Pass the callback to `run_workspace_filesystem_gc` from the completed monitor path instead of pre-gating with raw `docker compose down`.
5. Keep `_teardown_compose_stack` available for existing compatibility surface, but remove its post-merge filesystem-GC gate.

## Verification Commands and Pass Criteria

Focused tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_reaps_stale_volumes_when_compose_file_missing \
  tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_is_idempotent_when_nothing_left_to_reap \
  tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_fails_loud_when_label_probe_unavailable \
  tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_passes_volume_reaping_compose_teardown_to_filesystem_gc \
  tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails \
  -q
```

Focused static checks:

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/node/compose_manager.py \
  src/awf/runtime/pr_monitor_runner/lifecycle.py \
  tests/unit/node/test_compose_manager.py \
  tests/unit/runtime/test_monitor_completion_gc.py
```

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/node/compose_manager.py \
  src/awf/runtime/pr_monitor_runner/lifecycle.py
```

Focused coverage evidence:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_reaps_stale_volumes_when_compose_file_missing \
  tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_is_idempotent_when_nothing_left_to_reap \
  tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_fails_loud_when_label_probe_unavailable \
  tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_passes_volume_reaping_compose_teardown_to_filesystem_gc \
  tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails \
  --cov=awf.node.compose_manager \
  --cov=awf.runtime.pr_monitor_runner.lifecycle \
  --cov-report=term-missing \
  --no-cov-on-fail \
  -q
```
