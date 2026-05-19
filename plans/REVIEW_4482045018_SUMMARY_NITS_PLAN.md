# Review 4482045018 Summary Nits Plan

## Problem Statement And Scope

PR review comment `issue:4482045018` flags two remaining local-service env
handling issues:

- `src/awf/service/bootstrap.py` mirrors `AWF_DOCKER_HOST` into `DOCKER_HOST`
  for Docker subprocesses but leaves the AWF-internal key in the subprocess
  environment.
- `src/awf/service/config.py` may load `docker/compose/.env` adjacent to the
  default relative compose path from the current working directory without
  verifying that the directory is the AWF asset root.

Scope is limited to these two behaviors, their focused regression tests, and
this plan/validation record.

## Requirements Checklist

- Add/update a regression proving bootstrap Docker subprocess env contains
  `DOCKER_HOST` from `AWF_DOCKER_HOST` while suppressing `AWF_DOCKER_HOST`.
- Add/update a regression proving provider environment fallback ignores an
  unrelated current-directory `docker/compose/.env` when no AWF asset root
  validates it.
- Preserve explicit `compose_env_file` behavior for provider env resolution.
- Keep changes local to service env resolution and do not alter branch/push
  behavior.

## Implementation Steps

1. Update bootstrap tests to assert `AWF_DOCKER_HOST` is not forwarded after
   Docker host mirroring.
2. Add config/provider env tests for validated default compose env fallback and
   unrelated CWD suppression.
3. Implement bootstrap env suppression by popping `AWF_DOCKER_HOST` after
   deriving `DOCKER_HOST`.
4. Implement validated default compose env fallback in config using the verified
   AWF bootstrap asset root.
5. Run focused failing tests before implementation when practical, then rerun
   focused tests after the fix.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py tests/unit/service/test_config.py -q`
  - Passes with the new regressions.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py src/awf/service/config.py tests/unit/service/test_bootstrap.py tests/unit/service/test_config.py`
  - No lint regressions in touched files.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - No typing regressions in source files.
