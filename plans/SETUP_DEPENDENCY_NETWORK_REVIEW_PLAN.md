# Setup Dependency Network Review Plan

## Problem Statement And Scope

Address PR #248 review feedback for setup dependency network retry handling.
The review flags two outside-diff concerns:

- monitor recovery setup failures still finish recovery operations with the
  generic `MONITOR_RECOVERY_SETUP_FAILED` reason even when setup classified the
  root cause as `SETUP_DEPENDENCY_NETWORK_FAILURE`;
- the HTTP 5xx transient classifier has a broad `status code[:= ]+5xx` branch
  that can match non-HTTP numeric output.

Scope is limited to the setup dependency classifier, recovery operation failure
reason propagation, and focused regression tests.

## Requirements Checklist

- Preserve the precise `SETUP_DEPENDENCY_NETWORK_FAILURE` reason on recovery
  operation rows when setup dependency retry exhaustion is the root cause.
- Keep generic setup recovery failures using `MONITOR_RECOVERY_SETUP_FAILED`.
- Tighten HTTP 5xx classification so status-code matches require HTTP/index
  context, while preserving real package-index 5xx retry behavior.
- Do not weaken existing regression tests or broaden unrelated behavior.
- Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Add or update failing tests for precise recovery operation reason propagation
   and the HTTP 5xx false-positive shape.
2. Update the executor setup-failure recovery path to use the classified setup
   dependency reason when present.
3. Tighten the HTTP 5xx classifier regex to remove context-free status-code
   matching.
4. Run focused unit tests for runtime validation and monitor recovery.
5. Run narrow lint/type/test checks if the focused suite passes and time allows.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_monitor_recovery.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/runtime/test_validation.py tests/unit/control/test_executor_monitor_recovery.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if runtime permits.
