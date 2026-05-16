# PRRT_kwDOSJAM6s6CkJvb Compose Env File Plan

## Problem Statement

The local service bootstrap command loads `docker/compose/.env` for readiness and
provider checks, but the generated `docker compose -f docker/compose/local-service.yml`
stage commands do not pass that env file to Compose. If a repo-root `.env`
contains blank Compose-interpolated values, a rerun from a shell without exported
tokens can fail before the persisted `docker/compose/.env` values are considered.

## Scope

- Address review thread `PRRT_kwDOSJAM6s6CkJvb` only.
- Keep bootstrap command ordering and existing stage semantics intact.
- Preserve shell-only bootstrap behavior when no Compose env file exists.

## Requirements

- [ ] Add a regression proving bootstrap Compose stages pass `docker/compose/.env`
      when it exists.
- [ ] Ensure all Compose bootstrap stages share the same env-file behavior.
- [ ] Do not add `--env-file` to the separate agent runtime image build.
- [ ] Keep existing bootstrap tests green.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_bootstrap.py`.
2. Update `src/awf/service/bootstrap.py` to include the local Compose env file
   in generated Compose commands when the env file exists.
3. Run the new test first to confirm the current failure, then run the narrow
   bootstrap unit test module after the implementation.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::<new-test> -q`
  fails before implementation because no `--env-file` argument is generated.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q`
  passes after implementation.
