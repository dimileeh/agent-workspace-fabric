# Review 4482045018 Env Seed Plan

## Problem Statement And Scope

Address PR review comment `issue:4482045018` for local-service env-file seeding in `awf init`.

The scoped behaviors are:

- Preserve comment-only root `.env` overlay files when seeding `docker/compose/.env` from a compose env template.
- When `docker/compose/.env.example` is absent but both root `.env` and root `.env.example` exist, seed from the root template and overlay the existing root env values so template-only defaults are not dropped.

## Requirements Checklist

- Add regression coverage for comment-only overlay content.
- Add regression coverage for root `.env.example` template defaults surviving when root `.env` supplies overriding values.
- Keep existing compose `.env.example` precedence intact.
- Keep existing root `.env` value precedence intact.
- Do not weaken existing env-file failure, display-path, or service-command routing behavior.

## Implementation Steps

1. Add failing unit tests in `tests/unit/cli/test_init.py` for the two reviewed edge cases.
2. Update `_merge_env_seed_contents()` so trailing pending overlay context is appended when an overlay contains no assignments.
3. Update `_resolve_service_compose_paths()` and `_init_env_overlay_source()` so the root `.env.example` can serve as a seed template with root `.env` as an overlay when the compose-specific template is absent.
4. Run targeted tests for the updated init behavior.
5. Run the relevant lint/type/unit validation surface as practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/cli/test_init.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: targeted tests pass, lint passes, and mypy reports no errors.
