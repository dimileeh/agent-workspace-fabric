# PRRT_kwDOSJAM6s6F68kk Monitor Format Check Autofix Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F68kk_MONITOR_FORMAT_CHECK_AUTOFIX_PLAN.md`

## Requirement Status

- Add regression coverage for mixed normalizer and `awf-ruff-format-check` output:
  Complete. Added a focused regression in
  `tests/unit/runtime/test_pr_monitor_commit_autofix.py`.
- Prevent `Would reformat:` paths from entering the monitor restage-only retry
  path: Complete. `awf-ruff-format-check` is no longer classified as a
  deterministic restage-only repair hook in
  `src/awf/runtime/pr_monitor_runner/precommit_autofix.py`.
- Preserve deterministic normalizer restaging for hooks that actually modify files:
  Complete. Existing normalizer coverage still passes.
- Avoid broad AWF/GitHub-owned validation inside the agent phase: Complete. Only
  focused unit, lint, and format checks were run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/precommit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/PRRT_kwDOSJAM6s6F68kk_MONITOR_FORMAT_CHECK_AUTOFIX_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F68kk_MONITOR_FORMAT_CHECK_AUTOFIX_VALIDATION.md`

Focused checks:

- Before implementation, the two new/updated focused nodes failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_skips_formatter_check_repair_paths tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_skips_mixed_normalizer_and_format_check_paths -q`
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passed with 13 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
