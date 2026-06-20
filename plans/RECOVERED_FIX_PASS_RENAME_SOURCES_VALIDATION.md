# Recovered Fix-Pass Rename Sources Validation

Plan reference: `plans/RECOVERED_FIX_PASS_RENAME_SOURCES_PLAN.md`

## Requirement Status

- Use the existing `--name-status -z` changed-path parser for recovered fix-pass diffs:
  Complete. `pre_push_validation_fix_pass.py` now runs `git diff --name-status -z`
  and parses with `_changed_paths_from_name_status_z`.
- Include both rename source and destination paths when calling the recovered
  protected-scope checker: Complete. The new regression asserts
  `(".github/workflows/ci.yml", "docs/ci.yml")` is passed for an `R100` record.
- Preserve fail-closed behavior for malformed or unavailable recovered diff output:
  Complete. The parser raises `ProtectedScopeDiffError` for malformed output, which
  follows the existing recovered-diff-unavailable path.
- Avoid broad validation: Complete. Only the focused recovered-head unit tests were run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
- `plans/RECOVERED_FIX_PASS_RENAME_SOURCES_PLAN.md`
- `plans/RECOVERED_FIX_PASS_RENAME_SOURCES_VALIDATION.md`

Focused command run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`

Result: pytest passed, `5 passed, 6 deselected`; ruff passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation
and merge gating after agent completion.
