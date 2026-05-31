# PRRT_kwDOSJAM6s6F6y9i Monitor Autofix Validation

Plan reference: `PRRT_kwDOSJAM6s6F6y9i_MONITOR_AUTOFIX_PLAN.md`

## Requirement Status

- Complete: Skip monitor pre-commit autofix commit retries unless the commit failure is
  classified as `deterministic`.
- Complete: Preserve deterministic hook restaging for normalizer/formatter hook
  modifications.
- Complete: Add regression coverage for semantic Ruff autofix output that must not
  trigger a monitor restage/retry.
- Complete: Run only focused checks for the touched files; AWF/GitHub owns broad
  validation after agent completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/commit_autofix.py` to return no repair
  paths when `_classify_post_agent_commit_failure` chooses a non-deterministic repair
  strategy.
- Added `tests/unit/runtime/test_pr_monitor_commit_autofix.py` with focused coverage for
  semantic Ruff autofix output and deterministic hook-modified paths.
- Confirmed the new semantic Ruff regression failed before the source change:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  reported `_monitor_precommit_autofix_repair_paths` returning
  `("src/awf/mcp/server.py",)`.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
- Passed focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`

No broad AWF/GitHub-owned validation suite was run inside the agent phase.
