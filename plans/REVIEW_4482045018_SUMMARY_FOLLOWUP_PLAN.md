# Review 4482045018 Summary Follow-Up Plan

## Problem Statement And Scope

Address the review-level PR comment follow-up issues for Compose env integration:

- Make the Compose interpolation key cache miss check unambiguous with a dedicated sentinel.
- Avoid constructing a Docker subprocess environment when the caller environment already contains the resolved Compose interpolation value, even if the Compose env file is stale.
- Forward `compose_file` consistently from core readiness collection into service status collection.

Scope is limited to the service environment/logs/readiness paths and focused unit regression tests.

## Requirements Checklist

- [ ] Cache lookup distinguishes a missing cache entry from a stored cached value using an explicit sentinel.
- [ ] Logs Compose interpolation env calculation does not request an override when the caller environment already matches the resolved service value.
- [ ] Core readiness forwards `compose_file` to `status_collector` when `compose_env_file` is omitted.
- [ ] Core readiness forwards `compose_file` to `status_collector` when `compose_env_file` is explicitly provided, including explicit `None`.
- [ ] Narrow unit tests cover the behavior changes.
- [ ] Run focused tests for the changed service modules and at least a narrow lint/type check if practical.

## Implementation Steps

1. Add or update tests in `tests/unit/service/test_logs.py` and `tests/unit/service/test_readiness.py` to expose the unnecessary env override and missing `compose_file` forwarding.
2. Update `src/awf/service/environment.py` cache lookup to use a dedicated missing sentinel.
3. Update `compose_interpolation_environ` so a matching caller value suppresses stale env-file override emission.
4. Update `src/awf/service/readiness.py` to pass `compose_file` through every `status_collector` call path.
5. Run focused tests and validation commands, then document the result in the validation file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/service/test_readiness.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py src/awf/service/readiness.py tests/unit/service/test_logs.py tests/unit/service/test_readiness.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: focused tests pass, lint/type checks pass or any unrelated environmental blocker is documented.
