# Review 4482045018 Env Merge Validation

Plan reference: `plans/review_4482045018_env_merge_PLAN.md`

## Requirement Status

- Complete: Invalid UTF-8 in seed or overlay dotenv bytes now raises `_EnvSeedMergeError` with the operator-facing message `env seeding merge requires UTF-8 dotenv files`; `awf init --format json` includes that `merge_overlay` env error instead of silently writing template-only contents.
- Complete: Duplicate overlay-only keys now retain reusable section-documentation context before the final value while preserving existing behavior that drops stale single-value comments from overwritten root-only assignments.
- Complete: Compose interpolation key caching now uses a bounded ordered cache keyed by compose path, SHA-256 content digest, and byte size rather than the full YAML contents.
- Complete: Focused regression coverage was added for non-UTF-8 dotenv merge inputs, overlay-only duplicate context preservation, JSON init error surfacing, and digest-sized cache keys.
- Complete: Narrow validation commands passed.

## Evidence

Changed files:

- `src/awf/cli/main.py`
- `src/awf/service/environment.py`
- `tests/unit/cli/test_init.py`
- `tests/unit/service/test_logs.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_merge_env_seed_contents_rejects_non_utf8_dotenv_contents tests/unit/cli/test_init.py::test_merge_env_seed_contents_preserves_context_before_duplicate_overlay_only_key tests/unit/cli/test_init.py::test_init_without_path_json_marks_non_utf8_env_overlay_merge_failed tests/unit/service/test_logs.py::test_service_logs_cache_key_does_not_retain_compose_contents -q`
  - Result: passed, `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_logs.py -q`
  - Result: passed, `135 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/environment.py tests/unit/cli/test_init.py tests/unit/service/test_logs.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/cli/main.py src/awf/service/environment.py tests/unit/cli/test_init.py tests/unit/service/test_logs.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Remaining Gaps

None.
