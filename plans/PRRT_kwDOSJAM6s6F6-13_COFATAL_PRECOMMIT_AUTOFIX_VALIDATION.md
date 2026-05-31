# PRRT_kwDOSJAM6s6F6-13 Cofailed Pre-Commit Autofix Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F6-13_COFATAL_PRECOMMIT_AUTOFIX_PLAN.md`

## Requirement Status

- Add regression coverage for a deterministic normalizer hook co-failing with a
  non-deterministic hook while still returning only the normalizer repair paths:
  Complete. Added parser and retry-level coverage in
  `tests/unit/runtime/test_pr_monitor_commit_autofix.py`.
- Preserve the existing safety that semantic hook autofix output does not become
  eligible for the monitor restage-only retry path: Complete. The existing
  semantic Ruff autofix test still passes, and hook-local parsing only collects
  `Fixing ...` paths from deterministic normalizer hook blocks.
- Preserve the existing safety that `awf-ruff-format-check` `Would reformat:`
  paths do not become monitor restage-only repair paths: Complete. Mixed
  normalizer plus format-check coverage now asserts the normalizer path is kept
  while the format-check path remains excluded.
- Keep retry restaging bounded to parser-reported deterministic paths and
  existing dirty path safety checks: Complete. The retry helper remains
  unchanged; the new retry regression verifies restaging only the deterministic
  normalizer path even when the retried commit still fails.
- Run only focused local checks; AWF/GitHub owns broad validation after agent
  completion: Complete. Only focused unit and touched-file ruff checks were run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/precommit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/PRRT_kwDOSJAM6s6F6-13_COFATAL_PRECOMMIT_AUTOFIX_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F6-13_COFATAL_PRECOMMIT_AUTOFIX_VALIDATION.md`

Focused checks:

- Before implementation, the new focused regression nodes failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_keeps_normalizer_paths_when_format_check_cofails tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_keeps_normalizer_paths_when_semantic_hook_cofails tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_normalizer_when_other_hook_cofails -q`
- After implementation, those same focused regression nodes passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_keeps_normalizer_paths_when_format_check_cofails tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_keeps_normalizer_paths_when_semantic_hook_cofails tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_normalizer_when_other_hook_cofails -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passed with 15 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
