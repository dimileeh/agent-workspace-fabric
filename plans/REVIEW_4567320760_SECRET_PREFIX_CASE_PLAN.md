# Review 4567320760 Secret Prefix Case Plan

## Problem Statement And Scope

Greptile identified that host setup secret-value detection lowercases the `bearer `
sentinel but compares token prefixes against the original string. Uppercase variants
such as `SK-`, `GHP_`, or `XOXB-` can bypass `_looks_like_secret_value`.

Scope is limited to `awf.host_setup.config` secret scanning and a focused unit
regression test.

## Requirements Checklist

- Add a regression test proving uppercase token prefixes are rejected.
- Update secret-value prefix matching to be case-insensitive.
- Preserve sanitized error diagnostics and existing lower-case behavior.
- Run only focused local validation for the changed host setup behavior.

## Implementation Steps

1. Add a focused unit test under `tests/unit/service/test_host_setup_config.py`.
2. Run that test before implementation and confirm it fails.
3. Change `_looks_like_secret_value` to compare lowercased input against the
   lower-case prefix tuple.
4. Re-run the focused host setup test selection.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k uppercase`
  fails before the implementation and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "secret or host_setup_config_rejects_secret_values"`
  passes after the implementation.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
