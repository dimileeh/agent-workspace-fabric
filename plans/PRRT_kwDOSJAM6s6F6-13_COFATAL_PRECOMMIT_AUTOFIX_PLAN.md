# PRRT_kwDOSJAM6s6F6-13 Cofailed Pre-Commit Autofix Plan

## Problem Statement and Scope

The PR monitor pre-commit autofix parser currently returns no repair paths when
a deterministic normalizer hook modifies files and any other hook also fails in
the same pre-commit run. That leaves deterministic hook edits dirty instead of
allowing the monitor to restage those exact paths and retry the commit once.

Scope is limited to the PR monitor pre-commit autofix parser, its focused retry
coverage, and this plan/validation pair.

## Requirements Checklist

- Add regression coverage for a deterministic normalizer hook co-failing with a
  non-deterministic hook while still returning only the normalizer repair paths.
- Preserve the existing safety that semantic hook autofix output does not become
  eligible for the monitor restage-only retry path.
- Preserve the existing safety that `awf-ruff-format-check` `Would reformat:`
  paths do not become monitor restage-only repair paths.
- Keep retry restaging bounded to parser-reported deterministic paths and
  existing dirty path safety checks.
- Run only focused local checks; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add focused failing parser and retry regression coverage for deterministic
   normalizer output with a co-failed non-deterministic hook.
2. Update `monitor_precommit_autofix_repair_paths` to parse hook-local output
   blocks and collect `Fixing ...` paths only from deterministic normalizer hook
   blocks that actually report `files were modified by this hook`.
3. Keep semantic/format hook paths excluded, including mixed
   `awf-ruff-format-check` output.
4. Run the narrow failing tests first, then the focused unit file and lint/format
   checks for touched source/tests.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6F6-13_COFATAL_PRECOMMIT_AUTOFIX_VALIDATION.md`.

## Verification Commands and Pass Criteria

- Before implementation, the new focused regression nodes fail because no
  co-failed normalizer paths are returned.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passes.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py`
  passes.
- Full AWF/GitHub validation is intentionally not run inside the agent phase.
