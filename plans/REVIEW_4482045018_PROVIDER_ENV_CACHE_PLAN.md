# REVIEW_4482045018_PROVIDER_ENV_CACHE_PLAN

## Problem Statement and Scope

Address the remaining review-level concerns from PR comment `issue:4482045018`:

- `collect_service_status` should not hard-code `os.environ` as the base environment when it resolves provider auth from a caller-supplied Compose env file.
- service logs should not keep Compose interpolation keys in a process-global cache that can remain stale after the Compose file changes.

Scope is limited to the status provider-environment contract, logs Compose interpolation parsing, focused regression tests, and the required validation artifact.

## Requirements Checklist

- Add a caller-controlled environment input to `collect_service_status` for the fallback provider-environment resolution path.
- Preserve existing callers that already pass `provider_environ` explicitly.
- Ensure `run_service_logs` observes Compose-file interpolation variable changes at the same path across calls.
- Replace or adjust tests so they assert the corrected behavior without weakening existing safety checks.
- Run targeted unit tests for status and logs changes, plus lint/type checks as practical for the touched surface.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add failing regression tests:
   - status: a caller-provided base environment is used with `compose_env_file` when `provider_environ` is omitted.
   - logs: editing the same Compose file path changes forwarded interpolation values on a later call.
2. Update `collect_service_status` to accept `environ: Mapping[str, str] | None = None` and pass `os.environ if environ is None else environ` to `resolve_local_service_provider_environ`.
3. Remove the process-lifetime interpolation-key cache or replace it with invocation-scoped behavior so modified Compose files are reparsed.
4. Run the focused failing tests, then run the targeted unit module tests.
5. Create `plans/REVIEW_4482045018_PROVIDER_ENV_CACHE_VALIDATION.md` with requirement-by-requirement evidence.
6. Stage only changed files and commit with a conventional commit message for comment `4482045018`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status.py tests/unit/service/test_logs.py -q`
  - Passes with the new regressions.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/status.py src/awf/service/logs.py tests/unit/service/test_status.py tests/unit/service/test_logs.py`
  - Passes with no lint findings.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes or any failure is documented if unrelated to this change.
