# PRRT_kwDOSJAM6s6Ff7v0 Init Legacy Timeout Parse Plan

## Problem Statement and Scope

The hidden legacy `awf init` bootstrap timeout flags are rejected by the migration
path when `awf init` is run without a project path. Because the timeout options
are still parsed as ranged floats, invalid values such as `not-a-number` or
out-of-range negatives fail during Click/Typer conversion before the migration
error can emit the documented setup/start guidance and JSON payload.

Scope is limited to the public `awf init` CLI option parsing and focused CLI
regression coverage for the legacy timeout flags.

## Requirements Checklist

- Add a regression test proving no-path `awf init --timeout-seconds` with invalid
  or negative values reaches the migration error and reports the rejected flag.
- Cover the paired legacy `--poll-interval-seconds` timeout flag because it has
  the same parse-before-migration behavior.
- Preserve JSON migration payload behavior when invalid legacy timeout values are
  supplied with `--format json`.
- Parse these legacy timeout option values laxly because the values are no
  longer used, while preserving explicit flag detection.
- Do not run broad AWF/GitHub-owned validation; use focused CLI tests and
  targeted static checks only.

## Implementation Steps

1. Extend the existing no-path `awf init` legacy-flag tests with invalid and
   out-of-range timeout values.
2. Add a JSON-output regression for an invalid legacy timeout value.
3. Change the hidden timeout options in `src/awf/cli/main.py` from ranged floats
   to strings without Click numeric range validation.
4. Run the focused regression tests and targeted lint/type checks for touched
   files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py -q -k "legacy_bootstrap_flags or invalid_legacy_timeout"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init_parts/test_init_part_001.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/cli/main.py`
  passes.

Full AWF/GitHub validation remains owned by AWF after agent completion.
