# Plan: Address Review Comment 4314259708 Init Test Nitpicks

## Problem Statement And Scope

CodeRabbit's review-level comment flagged two brittle test patterns in
`tests/unit/cli/test_init.py`: an assertion against Typer's internal option
object for the `awf init` help text, and a write-failure helper that compares a
raw path string.

Scope is limited to keeping the same user-facing coverage while making these
tests less dependent on internal or relative-path formatting details. No
production behavior, branch changes, pushes, or unrelated refactors are in
scope.

## Requirements Checklist

- Replace the `inspect.signature(...).parameters["write_env"].default` help
  assertion with a behavior-level CLI help invocation.
- Assert the `init --help` output includes `docker/compose/.env`.
- Normalize both sides of the write-failure path comparison while preserving
  the existing synthetic `OSError` behavior.
- Remove imports made obsolete by the test change.
- Run focused lint and unit tests for the touched init CLI test surface.
- Write validation evidence in a matching validation document.
- Commit locally with a conventional message referencing comment `4314259708`.

## Implementation Steps

1. Update `test_init_write_env_help_names_compose_target` to invoke
   `app` through `_runner` with `["init", "--help"]`.
2. Update `_fail_path_write_bytes` to compare resolved `Path` values.
3. Run focused lint for `tests/unit/cli/test_init.py`.
4. Run the relevant init CLI tests.
5. Save validation results to `plans/comment_4314259708_VALIDATION.md`.
6. Stage only changed files and commit locally.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_init.py
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -k "write_env_help or env_write_fails or json_marks_env_write_failed" -q
```

Pass criteria: lint exits zero and the focused tests pass.

## TDD Note

This is a test-quality review fix, not a production behavior change. The new
help assertion should pass against the existing CLI behavior; the practical
check is that the updated test still validates the same public contract without
depending on Typer internals.
