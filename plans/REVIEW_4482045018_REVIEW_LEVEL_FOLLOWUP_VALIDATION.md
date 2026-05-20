# Review 4482045018 Review-Level Follow-Up Validation

Plan reference:
`plans/REVIEW_4482045018_REVIEW_LEVEL_FOLLOWUP_PLAN.md`

## Requirement Status

- Preserve the no-verified-root policy: Complete.
  Existing conservative behavior remains unchanged; current-directory `.env`
  can be read for settings but is not forwarded as Compose `--env-file` without
  a verified AWF source asset root.
- Avoid redundant bootstrap asset-root lookup after service path resolution:
  Complete.
  `src/awf/cli/main.py` now threads a trusted Compose env path from the
  already-resolved service paths into runtime env resolution, avoiding the
  second `_is_local_service_compose_env_path` lookup for service commands.
- Duplicate overlay-only key context observation: False positive.
  The attempted broader preservation conflicted with
  `test_init_without_path_deduplicates_root_only_overlay_keys`, which requires
  stale root-only duplicate context to be omitted. The merge policy was left
  intact.
- Add regression tests first and keep scope narrow: Complete.
  Added
  `test_service_logs_reuses_resolved_asset_root_for_compose_env_file` to prove a
  source-checkout service command uses one asset-root lookup while still passing
  the Compose env file.
- Commit locally without branch or push changes: Complete.

## Evidence

- Initial focused regression run failed before implementation:
  `test_service_logs_reuses_resolved_asset_root_for_compose_env_file` observed
  two asset-root lookups.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_reuses_resolved_asset_root_for_compose_env_file tests/unit/cli/test_init.py::test_init_without_path_deduplicates_root_only_overlay_keys -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py -q`
  passed: `190 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Iteration Notes

- Iteration 1 tried to preserve blank-separated single-comment context before a
  duplicate overlay-only key.
- Full CLI tests exposed the conflict with the existing stale-context
  deduplication regression.
- Iteration 2 restored the existing merge policy and retained only the
  asset-root lookup fix.
