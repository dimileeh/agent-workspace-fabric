# Comment 4567320760 Review Fix Plan

## Problem Statement And Scope

PR review comment `issue:4567320760` raised two host setup correctness concerns:

- `verified_source_from_metadata` detects stale source-checkout metadata by checking `len(stale_details) > 1`, which is fragile if future baseline diagnostic keys are added.
- `write_host_setup_config` reports write-time `OSError` failures as `HOST_SETUP_CONFIG_CORRUPT`, conflating malformed existing config files with inability to persist a config.

Scope is limited to `awf.host_setup` code, its public exports, focused unit regressions, and this plan/validation record.

## Requirements Checklist

- Add a dedicated `HOST_SETUP_CONFIG_WRITE_FAILED` reason code for write-time persistence failures.
- Preserve existing corrupt-config reason behavior for read/path-resolution validation failures unless the write operation reaches filesystem persistence and fails with `OSError`.
- Export the new reason code from the host setup public API.
- Replace the stale-metadata count sentinel with explicit divergence booleans that remain correct when baseline diagnostic keys are added.
- Add focused regression tests proving the new write-failure reason and the non-count-based stale decision.
- Avoid broad AWF/GitHub-owned validation; run only targeted local tests for the changed behavior.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_host_setup_config.py`:
   - write persistence failure returns `HOST_SETUP_CONFIG_WRITE_FAILED`;
   - source metadata revalidation does not raise stale solely because baseline details contain more than one key.
2. Update `src/awf/host_setup/config.py`:
   - define and use `HOST_SETUP_CONFIG_WRITE_FAILED` in the `write_host_setup_config` `OSError` branch;
   - include it in `__all__`.
3. Update `src/awf/host_setup/__init__.py` to re-export the new reason code.
4. Update `src/awf/host_setup/source_assets.py` to compute staleness from explicit `markers_stale` and `asset_paths_stale` booleans.
5. Run the focused unit test file or specific tests only.
6. Record results in `plans/COMMENT_4567320760_VALIDATION.md`.

## Verification Commands And Pass Criteria

- First-run targeted regressions after adding tests should fail for the expected reasons before implementation.
- Final focused command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
```

Pass criteria: the focused host setup unit tests pass. Full AWF/GitHub validation is intentionally left to AWF after agent completion per workspace contract.
