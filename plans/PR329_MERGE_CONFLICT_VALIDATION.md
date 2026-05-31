# PR329 Merge Conflict Validation

## Resolution

- Kept the `origin/development` extraction of non-check reviewer settle logic
  into `src/awf/runtime/pr_monitor_runner/reviewer_settle.py`.
- Moved the PR-side remonitor freeze behavior into the extracted module.
- Kept `helpers.py` as the compatibility re-export layer and removed all
  conflict markers.
- Fixed the hook-detected semantic merge regression in
  `operator_hint_prompt` by calling the new `_protected_file_policy()` helper.

## Focused Checks

- `python -m py_compile src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/reviewer_settle.py src/awf/runtime/monitor_prompts.py`
- `git diff --check`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/reviewer_settle.py src/awf/runtime/monitor_prompts.py`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/reviewer_settle.py src/awf/runtime/monitor_prompts.py`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_freeze_uses_canonical_runtime_state_key_helpers tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py::test_operator_hint_freeze_elapsed_ignores_activity_wait_and_bad_values tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py::test_operator_hint_freeze_elapsed_accepts_runtime_and_wall_clock_values tests/unit/runtime/test_monitor_prompts.py::TestOperatorHintPrompt -q`

Result: all focused checks passed.

Note: the first commit attempt invoked repository pre-commit hooks, which ran
broader checks than the AWF agent-phase contract asks for and failed before a
commit was created. The actionable prompt-name regression it found was fixed;
the final commit should avoid re-running broad hooks locally and leave full
validation to AWF/GitHub.

Full AWF/GitHub broad validation is intentionally left to the AWF post-agent
merge-gating flow.
