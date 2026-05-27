# PR Monitor Pre-Push Validation Validation

## What Changed

- PR monitor builders now receive the service `ValidationRunner` and pass it
  into `PullRequestMonitorRunner`.
- Comment repair, CI repair, and sync-base repair pushes now route through a
  validated push helper.
- The validated push helper records a validation run for the local head before
  push, with `workspace_head_sha` and `target_head_sha` set to the same commit.
- A bounded pre-push validation-fix pass can ask the monitor adapter to repair
  a validation failure, commit the fix, revalidate, and only then push.
- PR #288's missing-current-head fallback remains in place for PR heads that
  still lack AWF validation provenance.

## Tests Added

- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - successful pre-push validation records the pushed target head;
  - validation failure blocks push;
  - one validation-fix pass revalidates before push;
  - comment repair uses validated push and does not resolve threads on failure;
  - CI repair uses validated push;
  - sync-base repair uses validated push.

## Validation Run

- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_runner_parts tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts tests/unit/control/test_executor_parts tests/unit/control/test_executor_coverage_edges_parts tests/unit/control/test_executor_monitor_recovery_parts tests/unit/control/test_executor_error_paths_parts -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_worker.py tests/unit/runtime/test_release_pr_monitor.py tests/unit/runtime/test_pr_monitor_manual_merge.py::test_release_monitor_factory_uses_manual_merge_contract -q`
- `uv run --python 3.12 --extra dev pytest tests/unit -q -n 20`

Final post-rebase full unit result: `7936 passed in 531.92s`.
