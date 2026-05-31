# Review Issue 4585873052 Pre-Commit Autofix Cleanup Plan

## Problem Statement and Scope

Greptile's review-level PR comment called out two small readability issues in
the PR monitor pre-commit autofix retry code: `Would reformat:` repair paths are
not deduplicated at the same local collection point as `Fixing ...` paths, and a
thin parser shim in `commit_autofix.py` lacks documentation explaining why it
exists.

Scope is limited to the PR monitor pre-commit autofix parser, its retry helper
shim comment, focused parser coverage, and the required plan/validation files.

## Requirements Checklist

- Deduplicate `Would reformat:` repair paths locally in
  `precommit_autofix.py`, matching the existing `Fixing ...` path handling.
- Keep deterministic hook parsing behavior unchanged for monitor commit
  retries.
- Document why `_monitor_precommit_autofix_repair_paths` remains a thin shim.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add focused unit coverage documenting duplicate `Would reformat:` path
   handling for deterministic hooks.
2. Update `precommit_autofix.py` so `format_repair_files` uses the same local
   `dict.fromkeys` deduplication pattern as normalizer repair files.
3. Add a docstring to the `_monitor_precommit_autofix_repair_paths` shim in
   `commit_autofix.py`.
4. Run the focused PR monitor autofix unit test file and targeted lint on the
   changed files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/precommit_autofix.py src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Pass criteria: the focused unit test file and targeted lint pass. Full
AWF/GitHub validation remains owned by AWF after agent completion.
