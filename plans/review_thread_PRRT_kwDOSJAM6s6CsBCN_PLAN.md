# PRRT_kwDOSJAM6s6CsBCN Plan

## Problem Statement and Scope

The review reports that idempotency replay for legacy flat workspace creates
regressed after `requires_database=True` began coercing to `profile_ref="aira"`.
Rows created before that collapse may store only `requires_database=True` with
no `profile_ref` or `env_profile`, causing replay of the same legacy request to
conflict.

Scope is limited to workspace create idempotency profile matching and a focused
regression test.

## Requirements Checklist

- Preserve replay when an existing legacy row has `requires_database=True`,
  `profile_ref=None`, and `env_profile=None`, and the replay request resolves to
  the database compatibility profile.
- Keep non-database named profile requests from matching legacy rows with no
  profile identity.
- Keep current rich create and auto-profile idempotency behavior unchanged.
- Validate with the narrowest relevant unit test command.

## Implementation Steps

1. Add a failing regression test covering a legacy database row replayed by the
   coerced `profile_ref="aira"` request.
2. Update `_profile_ref_matches` to treat `requires_database=True` as the legacy
   durable identity for the database compatibility profile.
3. Run the focused workspace idempotency tests.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
  passes.
