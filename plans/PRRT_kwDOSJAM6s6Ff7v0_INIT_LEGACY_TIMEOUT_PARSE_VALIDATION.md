# PRRT_kwDOSJAM6s6Ff7v0 Init Legacy Timeout Parse Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Ff7v0_INIT_LEGACY_TIMEOUT_PARSE_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add a regression test proving no-path `awf init --timeout-seconds` with invalid or negative values reaches the migration error and reports the rejected flag. | Complete | Extended `tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_legacy_bootstrap_flags_with_migration`; the new cases failed before implementation and pass after the parser change. |
| Cover the paired legacy `--poll-interval-seconds` timeout flag because it has the same parse-before-migration behavior. | Complete | Added invalid and out-of-range poll interval cases to the same no-path legacy-flag regression. |
| Preserve JSON migration payload behavior when invalid legacy timeout values are supplied with `--format json`. | Complete | Added `test_init_without_path_json_rejects_invalid_legacy_timeout_with_migration`, which verifies the JSON reason code and rejected flag. |
| Parse these legacy timeout option values laxly because the values are no longer used, while preserving explicit flag detection. | Complete | Changed hidden legacy timeout options in `src/awf/cli/main.py` from ranged floats to strings; existing explicit-flag detection continues to drive the rejection path. |
| Do not run broad AWF/GitHub-owned validation; use focused CLI tests and targeted static checks only. | Complete | Ran only focused CLI regressions plus targeted ruff and mypy checks listed below. Full AWF/GitHub validation remains managed by AWF after agent completion. |

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py -q -k "legacy_bootstrap_flags or invalid_legacy_timeout"`: passed, `11 passed, 45 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_004.py -q -k "explicit_default_bootstrap_flags"`: passed, `1 passed, 16 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init_parts/test_init_part_001.py`: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/cli/main.py`: passed.
- `git diff --check`: passed.

No remaining gaps.
