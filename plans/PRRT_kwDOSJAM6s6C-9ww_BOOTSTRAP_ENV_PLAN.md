# PRRT_kwDOSJAM6s6C-9ww Bootstrap Env Plan

## Problem Statement And Scope

PR #262 review thread `PRRT_kwDOSJAM6s6C-9ww` reports that bootstrap Docker
subprocesses receive only the explicit `provider_environ` mapping. When callers
pass a partial mapping such as `{"COMPOSE_PROFILES": "ollama-bridge"}`, the
subprocess environment drops inherited host variables including `PATH`, `HOME`,
and `DOCKER_HOST`.

Scope is limited to local service bootstrap environment construction and its
unit coverage.

## Requirements Checklist

- Preserve the existing local service environment when `provider_environ` is not supplied.
- Merge explicit `provider_environ` values over the local service environment when supplied.
- Keep `provider_environ` values available for bootstrap stage selection and readiness polling.
- Add regression coverage for partial `provider_environ` inheritance.
- Run the narrow bootstrap unit tests needed to prove the change.

## Implementation Steps

1. Add a failing unit test showing a partial `provider_environ` keeps inherited local service env keys.
2. Update bootstrap environment construction to merge `local_service_environ()` with explicit overrides.
3. Run the targeted bootstrap unit tests.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q
```

Pass criteria: all bootstrap unit tests pass, including the new regression.
