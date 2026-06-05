# PR403 Shard And Comment Fix Plan

## Problem Statement

PR #403's new sharded CI run exposed failing coverage shards, and the PR has
two unresolved review threads:

- The local service env reader now preserves raw `${...}` literals too broadly,
  so host-side `awf setup/status/start` can disagree with Docker Compose for
  unquoted or double-quoted `.env` interpolation.
- General host CLI calls still do not discover the root Compose default
  `AWF_API_TOKEN=local-dev-token`, even though the API requires it in a fresh
  raw Compose stack.

## Scope

- Fix failing coverage shards without weakening maintainability guards.
- Restore Compose-compatible interpolation for local Compose `.env` reads while
  preserving literal single-quoted secrets produced by env migration.
- Teach shared CLI auth headers to discover the local service token default,
  while preserving explicit `--api-token` and shell `AWF_API_TOKEN` precedence.
- Reply to and resolve the addressed PR review threads after pushing the fix.

## Implementation Steps

1. Inspect shard logs for every failed coverage shard.
2. Split or trim any over-limit test file so the line-limit guard passes.
3. Add failing tests for Compose env interpolation semantics:
   - unquoted `${PORT:-9000}` expands,
   - unquoted `${HOME}/...` expands from the caller environment,
   - single-quoted `secret-${TOKEN_SUFFIX}` remains literal.
4. Add failing CLI tests proving `_api_token_headers(None)` resolves the local
   Compose default token when shell `AWF_API_TOKEN` is absent.
5. Implement the smallest shared helpers needed for those tests.
6. Validate with focused unit tests plus lint/format/whitespace checks.
7. Commit, push, refresh CI/comment state, and resolve the review threads.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py tests/unit/service/test_config_parts/test_config_part_003.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_common.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py src/awf/service/environment.py src/awf/cli/common.py tests/unit/service/test_environment.py tests/unit/service/test_config_parts/test_config_part_001.py tests/unit/service/test_config_parts/test_config_part_003.py tests/unit/cli/test_common.py tests/unit/test_core_decomposition_maintainability.py`
- `uv run --python 3.12 --extra dev ruff format --check` on touched Python files.
- `git diff --check`

## Pass Criteria

- Failed shard root causes are addressed locally.
- Review-comment behaviors have regression tests.
- General CLI protected calls can use the local Compose default token in a
  fresh source checkout.
- Compose-style `.env` references are no longer treated as raw literals unless
  single-quoted.
