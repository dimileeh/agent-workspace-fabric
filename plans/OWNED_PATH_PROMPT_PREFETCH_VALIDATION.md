# Owned Path Prompt Prefetch Validation

Plan reference: `OWNED_PATH_PROMPT_PREFETCH_PLAN.md`

## Requirement Status

- Fetch prompt `owned_paths` once per `_run_fix_cycle` invocation: Complete.
  `_run_fix_cycle` now loads owned paths once after early repair preflight exits.
- Pass the fetched paths to every thread and review-comment prompt in that fix
  cycle: Complete. The loaded list is passed to `_address_thread` and
  `_address_review_comment_result`.
- Preserve existing direct-call behavior for `_address_thread`,
  `_address_review_comment`, and `_address_review_comment_result`: Complete.
  The helpers still fetch from `_owned_paths_for_prompt` when no paths are
  provided.
- Add a focused regression proving a multi-item fix cycle performs one owned
  path prompt load: Complete.
  `test_fix_cycle_fetches_prompt_owned_paths_once_for_comment_batch` covers a
  mixed batch of inline threads and review-level comments.
- Avoid broad AWF/GitHub-owned validation: Complete. Only targeted checks were
  run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/comments.py`
- `src/awf/runtime/pr_monitor_runner/fix_cycle.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
- `plans/OWNED_PATH_PROMPT_PREFETCH_PLAN.md`
- `plans/OWNED_PATH_PROMPT_PREFETCH_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_fix_cycle_fetches_prompt_owned_paths_once_for_comment_batch -q`
  - Passed: `1 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passed: `7 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  - Passed
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py::test_fix_cycle_returns_failed_push_when_thread_fix_hits_policy_block -q`
  - Passed: `1 passed`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/fix_cycle.py`
  - Passed

Full AWF/GitHub validation was not executed in the agent phase. AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
