# Mirror Hooks Repair Race Hardening Validation

## Commands
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  - Result: passed, `47 passed in 12.07s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py src/awf/runtime/pr_monitor_runner/mirror_hooks.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/git_manager.py src/awf/runtime/pr_monitor_runner/mirror_hooks.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
  - Result: passed, no issues in 9 source files.

## Coverage Notes
- Added Git manager coverage proving mirror hooks repair waits on the shared mirror lock.
- Added Git manager coverage proving disappearing linked-worktree metadata is pruned and retried once.
- Extended PR monitor pre-push validation coverage so mirror repair failures preserve operation, return code, stderr, stdout, stage, and mirror path details.
