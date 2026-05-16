# CI Workspaces Idempotency Replay Plan

## Problem Statement and Scope

PR #256 reports `python-full-coverage` failures in two workspace API unit tests:

- `TestWorkspaceCreateProviderReadinessPreflight::test_v2_warm_cache_replay_uses_durable_auto_profile_match`
- `TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded`

The reported nodes pass when run in isolation locally, so the scope is to find and fix the order-dependent or environment-dependent bug that makes these assertions fail under the CI test surface. The fix must preserve CI checks and test coverage; no skips, xfails, or weakened assertions.

## Requirements Checklist

- Reproduce the failure with the narrowest practical local command.
- Identify the root cause in production code or test isolation.
- Add or update regression coverage for the behavior involved.
- Keep the change scoped to workspace API idempotency/rate-limit behavior.
- Run focused verification for the failing nodes and enough surrounding tests to prove the leak is fixed.
- Commit locally with a conventional commit message that names the failed check and root cause.

## Implementation Steps

1. Run the reported pytest nodes first, then expand to `tests/unit/api/test_workspaces.py` or selected neighboring tests if the isolated nodes pass.
2. Inspect workspace API route helpers for process-global caches, rate-limit stores, provider readiness caches, and idempotency replay logic.
3. Patch the smallest source or fixture isolation issue that explains the order-dependent failure.
4. Add or adjust tests so durable idempotency replay and fresh-key rate limiting remain covered without relying on leaked global state.
5. Create `plans/ci_workspaces_idempotency_replay_VALIDATION.md` with requirement status and command evidence.
6. Commit the implementation and plan/validation artifacts locally.

## Verification Commands and Pass Criteria

Focused repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_warm_cache_replay_uses_durable_auto_profile_match tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q
```

Expanded workspace API surface:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q
```

Static checks if production code changes:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: all targeted and expanded commands pass, and any remaining unrun broader CI surface is documented in validation.

## Assumptions/Changes

- The warm-cache replay failure reproduced with an empty `AWF_HOST_HOME`: the test depended on ambient local Codex auth after its class fixture cleared provider auth environment variables.
- The rate-limit failures are fixed-window clock sensitive: a slow workspace create can cross the 60-second request-admission window before the second request, so tests that assert quota behavior need a pinned limiter clock.
- The implementation scope remains test isolation for workspace API behavior; production route semantics are unchanged.
