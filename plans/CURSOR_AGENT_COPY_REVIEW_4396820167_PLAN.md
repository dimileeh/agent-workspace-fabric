# Cursor Agent Copy Review 4396820167 Plan

## Problem Statement and Scope

PR review comment `4396820167` reports that the agent runtime Dockerfile should
not expose `cursor-agent` through a symlink to the installer location because
non-root execution can be sensitive to installer-owned path permissions. Scope
is limited to the Cursor installer block in `docker/agent-runtime.Dockerfile`,
its focused static Dockerfile unit test, and the required plan/validation
artifacts.

## Requirements Checklist

- Update the Dockerfile regression test first to require copying
  `cursor-agent` into `/usr/local/bin` instead of symlinking it.
- Confirm the updated focused test fails against the current Dockerfile when
  practical.
- Replace the symlink with a copied executable in the Dockerfile while keeping
  the existing installer location check and non-root `agent` smoke check.
- Run focused validation only; full AWF/GitHub validation remains managed by
  AWF after agent completion.
- Commit the local fix with a conventional commit referencing review comment
  `4396820167`.

## Implementation Steps

1. Change `tests/unit/test_agent_runtime_dockerfile.py` to assert a copy-based
   install and reject the symlink.
2. Run the focused Dockerfile unit test and capture the expected failure.
3. Change `docker/agent-runtime.Dockerfile` to install the copied binary with
   executable permissions in `/usr/local/bin`.
4. Re-run the focused Dockerfile unit test.
5. Write `plans/CURSOR_AGENT_COPY_REVIEW_4396820167_VALIDATION.md` with the
   requirement-by-requirement result and focused command evidence.
6. Stage only changed files and commit locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  should fail after the test-only edit and pass after the Dockerfile fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  should pass after the Dockerfile fix.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  should pass after the test edit.
- No full coverage, whole-repository test suite, frontend build, push, rebase,
  or branch switch will be run in the agent phase.

## Assumptions/Changes

- The original `-k cursor` selector was attempted after the test-only edit but
  selected zero tests because the existing test name does not include `cursor`.
  Verification therefore uses the exact Dockerfile test function that contains
  the Cursor assertions.
