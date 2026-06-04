# PR403 Coverage Gate Fix Plan

## Goal

Bring the PR 403 `python-full-coverage` job back above the 99% combined
coverage gate after the Compose env parsing and migration changes.

## Root Cause

The sharded test jobs passed, but the final combined coverage job failed at
98.97%. The downloaded `full-coverage-report` artifact shows the largest
new gaps in PR-touched runtime code:

- `src/awf/service/environment.py`, especially Compose env-file parsing,
  interpolation operators, invalid brace handling, and include path shapes.
- `src/awf/service/env_migration.py`, especially formatting and edge-case
  migration branches.

These are real runtime paths, so the primary fix is tests rather than global
coverage exclusions.

## Implementation

1. Add focused unit tests for Compose env-file parsing branches:
   - blank assignments and malformed/export-only lines;
   - single-quoted values with trailing and non-quote backslashes;
   - double-quoted trailing backslash;
   - inline comments;
   - lone dollar signs, unclosed braces, invalid brace expressions;
   - Compose `-`, `+`, `:+`, and mandatory `?` operator behavior.
2. Add focused unit tests for Compose include path parsing shapes:
   - invalid YAML propagation;
   - non-mapping documents;
   - mapping `include.path` sequences;
   - directory include targets and interpolated include skips.
3. Add focused unit tests for env migration branches:
   - invalid/unset keys ignored;
   - template comments and unmatched keys preserved;
   - missing-key insertion after unterminated existing env files;
   - conflict-only migrations;
   - formatting of quoted values, escaped dollars, newlines, tabs, and backup
     name collisions.
4. Use coverage exclusions only if a gap is proven to be non-runtime or
   intentionally untestable. No exclusion is expected for this fix.

## Validation

Run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py tests/unit/service/test_env_migration.py -q
uv run --python 3.12 --extra dev coverage erase
uv run --python 3.12 --extra dev coverage run --branch -m pytest tests/unit/service/test_environment.py tests/unit/service/test_env_migration.py
uv run --python 3.12 --extra dev coverage report -m src/awf/service/environment.py src/awf/service/env_migration.py
uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py src/awf/service/env_migration.py tests/unit/service/test_environment.py tests/unit/service/test_env_migration.py
```
