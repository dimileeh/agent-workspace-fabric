# Address Review Comment 4586615053 Plan

## Problem Statement and Scope

PR review comment `issue:4586615053` reports three Cursor runtime follow-ups:

- remove the unused private `_cursor_selected_model` wrapper in
  `src/awf/adapters/cursor.py`;
- consider making the Dockerfile `cursor-agent --version` check soft with
  `|| true`;
- consider hardcoding the Node symlink source to `/usr/bin/node`.

The Dockerfile suggestions are outside the immediate adapter behavior and the
current unit tests explicitly assert the existing Dockerfile semantics. Per the
task safety policy, existing regression assertions are policy evidence and must
not be rewritten or weakened only to satisfy reviewer feedback.

## Requirements Checklist

- [ ] Confirm `_cursor_selected_model` has no callers before removing it.
- [ ] Remove only the dead Cursor adapter wrapper and preserve
  `_cursor_model_for_effort`, which tests import directly.
- [ ] Do not change Dockerfile behavior when existing regression assertions
  require the current hard `cursor-agent --version` check and
  `command -v node` symlink source.
- [ ] Run focused checks only; AWF/GitHub own broad validation after agent
  completion.
- [ ] Commit the scoped local changes with a conventional commit message.

## Implementation Steps

1. Search for `_cursor_selected_model` references.
2. Delete the unused wrapper from `src/awf/adapters/cursor.py`.
3. Re-run the reference search to confirm the helper is gone.
4. Run targeted tests covering the Cursor adapter and current Dockerfile
   regression assertions.
5. Record validation evidence in
   `plans/ADDRESS_REVIEW_4586615053_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `rg -n "_cursor_selected_model" src tests docker`
  - Pass: no matches remain.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/cursor.py`
  - Pass: lint succeeds for the changed adapter file.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Pass: targeted Cursor adapter and Dockerfile regression tests pass.

Broad repository validation, coverage, and CI-equivalent checks are intentionally
left to AWF/GitHub after the agent phase.
