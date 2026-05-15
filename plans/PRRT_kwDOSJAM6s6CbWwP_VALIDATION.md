# PRRT_kwDOSJAM6s6CbWwP Callback Replay Admission Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CbWwP_PLAN.md`

## Requirement Status

- Complete: callbacks disabled, missing/invalid `Idempotency-Key`, and in-memory response replay behavior remain unchanged by the route ordering change.
- Complete: unknown fresh keys rejected by callback registration admission no longer call `CallbackService.replay_existing`.
- Complete: process-known keys can still perform durable replay before admission when the response replay cache has been replaced.
- Complete: admitted requests with keys not found in process-local caches still use durable replay/conflict handling before registration.
- Complete: the focused callback regression now asserts that only the admitted first key reaches durable replay, with direct coverage for the new positive replay-key cache.
- Complete: the change is ready for a local conventional commit on the existing AWF branch.

## Evidence

Files changed:

- `src/awf/api/routes/callbacks.py`
- `tests/unit/api/test_callbacks.py`
- `plans/PRRT_kwDOSJAM6s6CbWwP_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CbWwP_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss -q
# Failed before implementation: fresh rejected key still reached replay_existing.

uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss -q
# 1 passed

uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_db_replay_bypasses_limit_when_replay_cache_is_cold -q
# 1 passed

uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q
# 73 passed

uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 158 source files
```

Remaining gaps: none.
