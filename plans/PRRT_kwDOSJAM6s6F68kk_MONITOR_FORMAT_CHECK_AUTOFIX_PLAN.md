# PRRT_kwDOSJAM6s6F68kk Monitor Format Check Autofix Plan

## Problem Statement and Scope

The PR monitor pre-commit autofix parser treats `awf-ruff-format-check` as eligible
for the restage-only retry path. When a normalizer hook modifies files in the same
pre-commit run, the shared "files were modified by this hook" marker lets the parser
return `Would reformat:` paths even though the monitor retry does not run a
formatter. This can retry a commit that is still unformatted.

Scope is limited to the PR monitor autofix parser and its focused unit coverage.

## Requirements Checklist

- Add regression coverage for mixed normalizer and `awf-ruff-format-check` output.
- Prevent `Would reformat:` paths from entering the monitor restage-only retry path.
- Preserve deterministic normalizer restaging for hooks that actually modify files.
- Avoid broad AWF/GitHub-owned validation; run only focused checks for the touched
  behavior.

## Implementation Steps

1. Update `tests/unit/runtime/test_pr_monitor_commit_autofix.py` with a failing
   mixed-hook regression.
2. Update `src/awf/runtime/pr_monitor_runner/precommit_autofix.py` so
   `awf-ruff-format-check` is not classified as a deterministic restage-only repair
   hook.
3. Run the focused unit test file, or a narrower node first if practical.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run inside the agent phase; AWF
  owns that after completion.
