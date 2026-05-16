# Review Thread PRRT_kwDOSJAM6s6Ce2hw Plan

## Problem Statement and Scope

The workspace create rate-limit rejection path warms the replay-key cache by
loading every persisted workspace idempotency key. A fresh or unknown key after
process start can therefore trigger an unbounded full-table read before the
request is rejected. The fix is limited to workspace create idempotency replay
handling after admission rejection for both v1 and v2 create routes.

## Requirements Checklist

- Preserve idempotent replay behavior for existing workspace keys when the
  in-memory replay-key cache is cold and the create request is rate-limited.
- Preserve rate-limit rejection for fresh workspace keys.
- Avoid calling the unbounded workspace idempotency-key listing method from the
  post-rejection replay path.
- Check at most the submitted idempotency key before attempting locked replay.
- Add or update regression coverage for the bounded post-rejection behavior.

## Implementation Steps

1. Add a regression test proving a rate-limited fresh key does not call
   `WorkspaceRepository.list_idempotency_replay_keys`.
2. Add an exact-key repository probe for workspace idempotency keys.
3. Update the v1 and v2 post-rejection replay helpers to use the exact-key
   probe before the existing locked replay response.
4. Keep successful replays warming the in-memory replay-key cache.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py src/awf/db/repositories.py tests/unit/api/test_workspaces.py`
  passes.
