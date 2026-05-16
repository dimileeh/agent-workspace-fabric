# PRRT_kwDOSJAM6s6Cb3EE Callback Durable Replay Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Cb3EE_CALLBACK_DURABLE_REPLAY_PLAN.md`

## Requirement Status

- Complete: Existing in-memory response replay behavior is preserved; the
  existing cold response-cache replay regression still passes.
- Complete: Fresh over-quota keys are still rejected before
  `CallbackService.replay_existing()`; the existing focused regression still
  records only the admitted first key.
- Complete: Persisted callback replays now bypass callback registration rate
  limiting after both route replay caches are cold by warming positive replay
  keys from durable callback rows on limiter rejection.
- Complete: The default positive replay-key cache no longer evicts accepted
  keys at the response replay-cache entry limit; explicit bounded cache tests
  still cover LRU behavior.
- Complete: Idempotency conflict behavior remains routed through the existing
  `409 IDEMPOTENCY_CONFLICT` helper.
- Complete: Added API and repository regressions for durable replay-key loading
  and cold-cache replay.
- Complete: The fix is ready for a local conventional commit on the existing
  AWF branch.

## Evidence

Failing-first checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_db_replay_bypasses_limit_when_replay_caches_are_cold tests/unit/api/test_callbacks.py::test_callback_replay_key_cache_default_retains_keys_past_response_cache_limit -q
# Failed before implementation: replay returned 429 and the default key cache evicted the first key.

uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys -q
# Failed before implementation: CallbackSubscriptionRepository had no durable replay-key list method.
```

Passing verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_db_replay_bypasses_limit_when_replay_caches_are_cold tests/unit/api/test_callbacks.py::test_callback_replay_key_cache_default_retains_keys_past_response_cache_limit -q
# 2 passed

uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys -q
# 1 passed

uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss -q
# 1 passed

uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q
# 77 passed

uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py -q
# 16 passed

uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q
# 51 passed

uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/service/callbacks.py src/awf/db/repositories.py tests/unit/api/test_callbacks.py tests/unit/db/test_callback_repository.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/api/routes/callbacks.py src/awf/service/callbacks.py src/awf/db/repositories.py
# Success: no issues found in 3 source files
```

## Gaps

None.
