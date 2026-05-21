# Linux Runtime Ownership Repair Validation

## Plan Alignment

- Added runtime ownership repair before profile setup for normal execution and
  PR-monitor validate-only recovery.
- Added PR monitor dirty-worktree repair before `git add -A` and after
  `git commit`, covering successful and failed pre-commit hook runs.
- Kept the existing Git writability preflight after setup for Git object/ref
  writes.
- Documented `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED` in the reason catalog and
  doctor reason text.
- Documented that runtime directories such as `.venv` are covered by repeated
  per-worktree repair.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py::test_runtime_ownership_repair_runs_before_recovery_setup tests/unit/control/test_executor_monitor_recovery.py::test_runtime_ownership_repair_failure_blocks_recovery_setup tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_repairs_runtime_ownership_around_commit tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_stops_before_add_when_runtime_repair_fails -q`
  - Result: passed, 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q -k "ownership or writable or chown or venv" tests/unit/control/test_executor_monitor_recovery.py::test_runtime_ownership_repair_runs_before_recovery_setup tests/unit/control/test_executor_monitor_recovery.py::test_runtime_ownership_repair_failure_blocks_recovery_setup tests/unit/control/test_executor_coverage_edges.py::test_agent_git_writability_preflight_runs_inside_agent_container tests/unit/control/test_executor_coverage_edges.py::test_agent_git_writability_preflight_fails_when_repair_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_repairs_runtime_ownership_around_commit tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_stops_before_add_when_runtime_repair_fails`
  - Result: passed, 17 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py tests/unit/service/test_doctor_reasons.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_dirty_worktree_helper_returns_false_for_non_commit_cases tests/unit/runtime/test_pr_monitor_runner.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_branches -q`
  - Result: passed, 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/control/test_executor_monitor_recovery.py tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner.py -q`
  - Result: passed, 534 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit -q`
  - Result: passed, 7184 tests.

## Operational Checks

- `docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .`
  - Result: passed.
- `docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml build`
  - Result: passed.
- `uv run --python 3.12 --extra dev awf service bootstrap --timeout-seconds 300`
  - Result: passed; service status `ok`.
- Reattached PR #270 with Codex `gpt-5.5`, `xhigh`, and `auto_merge=true`.
  - Result: workspace `ws_12a7dd5bab03420d9dc65e57`, `attached_existing=false`,
    status `monitoring_pr`.
- Live Linux ownership smoke in `ws_12a7dd5bab03420d9dc65e57`:
  - Created a root-owned real `.venv` in the agent workspace.
  - Ran `repair_agent_writable_worktree(None, worktree_path)` from the
    control-plane worker container.
  - Confirmed `.venv` and `.venv/bin` became UID/GID `1000:1000`.
  - Ran `uv sync --extra dev` as `uid=1000(agent)` in the workspace container.
  - Result: passed.
- `uv run --python 3.12 --extra dev awf service status --format pretty`
  - Result: service status `ok`; PR #270 monitor workspace is the active open
    network-posture example.
