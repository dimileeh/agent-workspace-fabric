# Review 4482045018 Env Merge Docker Clear Validation

Plan reference: `plans/REVIEW_4482045018_ENV_MERGE_DOCKER_CLEAR_PLAN.md`

## Requirement Status

- Add regression coverage for blank-only seed preambles preserving the overlay header: Complete. Added `test_merge_env_seed_preserves_overlay_header_when_seed_starts_with_blank_line`.
- Add regression coverage for case-insensitive seed/overlay key matching: Complete. Added `test_merge_env_seed_matches_overlay_keys_case_insensitively`.
- Replace the Docker CLI empty-string scrub-marker contract with an explicit cleared-key API: Complete. Added `cleared_docker_cli_client_keys()` and updated `run_service_logs()` env construction.
- Preserve existing comment-ordering and overlay-only-key behavior: Complete. The touched `test_init.py` suite passes alongside the new cases.
- Commit only the changed files on the current AWF-managed branch: Complete pending local commit for this fix cycle.

## Evidence

- Pre-implementation regression check: `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_logs.py -q` failed during collection because the new Docker helper test imported the not-yet-implemented explicit cleared-key API.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_logs.py -q` passed with `144 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed with no issues.

## Gaps

None.
