# PRRT_kwDOSJAM6s6F6y9i Monitor Autofix Plan

## Problem Statement and Scope

The PR monitor dirty-worktree commit retry currently restages hook-modified paths when
`classification.autofix_repair_files` is non-empty, even if
`classification.repair_strategy` is `agent`. The review thread reports that this can
retry a commit that semantic hooks are still expected to reject.

Scope is limited to the PR monitor pre-commit autofix retry helper and focused unit
coverage for that behavior.

## Requirements Checklist

- Skip monitor pre-commit autofix commit retries unless the commit failure is classified
  as `deterministic`.
- Preserve deterministic hook restaging for normalizer/formatter hook modifications.
- Add regression coverage for semantic Ruff autofix output that must not trigger a
  monitor restage/retry.
- Run only focused checks for the touched files; AWF/GitHub owns broad validation after
  agent completion.

## Implementation Steps

1. Add a focused failing unit test for semantic Ruff autofix output.
2. Update `_monitor_precommit_autofix_repair_paths` to return no repair paths unless
   `classification.repair_strategy == "deterministic"`.
3. Run targeted pytest for the new/changed behavior.
4. Run targeted ruff on the touched source and test files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  - Passes with the semantic autofix regression covered.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  - Passes with no lint findings.
