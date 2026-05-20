# Comment 4482045018 Plan

## Problem Statement

Address PR review comment `issue:4482045018`, which reports two narrow follow-ups in the local service env seeding/bootstrap work:

- dotenv merge drops the first single-comment context block for duplicate root-only overlay keys;
- `StatusCollector` no longer reflects the full `collect_service_status` compose-context signature.

## Requirements Checklist

- Add regression coverage for first-occurrence duplicate root-only overlay key context with a single adjacent comment.
- Preserve last-value dotenv semantics while retaining relevant operator context for that duplicate key case.
- Add regression coverage proving bootstrap status collection receives the active compose context.
- Update bootstrap status collector typing/calls so `environ`, `compose_file`, and `compose_env_file` are represented and forwarded consistently.
- Keep changes scoped to the reported review comment and preserve existing env seeding safety tests.

## Implementation Steps

1. Add failing tests in `tests/unit/cli/test_init.py` and `tests/unit/service/test_bootstrap.py`.
2. Update `_merge_env_seed_contents_with_overlay_keys` to retain first duplicate root-only context without changing final value semantics.
3. Update `StatusCollector`, `_poll_status`, and the bootstrap call path to carry compose status context.
4. Adjust narrow test doubles only where the new status collector call shape requires it.
5. Run targeted tests, then run lint/type checks over touched Python surfaces.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_merge_env_seed_keeps_single_comment_before_duplicate_overlay_only_key tests/unit/service/test_bootstrap.py::test_bootstrap_forwards_compose_context_to_status_collector -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_bootstrap.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/cli/test_init.py tests/unit/service/test_bootstrap.py`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/cli/main.py src/awf/service/bootstrap.py tests/unit/cli/test_init.py tests/unit/service/test_bootstrap.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

## Pass Criteria

- New regressions fail before implementation when practical.
- Targeted and relevant unit tests pass.
- Ruff and mypy pass for the touched source/test surface.
