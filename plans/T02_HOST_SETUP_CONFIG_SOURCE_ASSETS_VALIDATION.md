# T02 Host Setup Config Source Assets Validation

Plan reference: `plans/T02_HOST_SETUP_CONFIG_SOURCE_ASSETS_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Config read/write round-trips through injected path | Complete | `tests/unit/service/test_host_setup_config.py::test_host_setup_config_round_trips_with_conservative_permissions` |
| Config parent directory uses owner-only permissions where supported | Complete | Same test asserts `0700` on POSIX |
| Config file uses owner-only permissions where supported | Complete | Same test asserts `0600` on POSIX |
| Config stores only non-secret setup settings | Complete | `src/awf/host_setup/config.py` models cover install channel, API host port, work dir, provider refs/status, client status, consent, and source metadata |
| Raw secret-looking values and secret-bearing keys are rejected | Complete | `test_host_setup_config_rejects_secret_values` covers model construction and YAML load rejection |
| Corrupt or schema-invalid YAML has `HOST_SETUP_CONFIG_CORRUPT` diagnostics | Complete | `test_corrupt_config_has_reason_code_and_path_details` |
| Valid AWF source checkout returns immutable handoff with resolved paths | Complete | `test_valid_source_checkout_returns_verified_asset_paths` |
| Invalid checkout reports `SOURCE_CHECKOUT_INVALID` and missing marker details | Complete | `test_invalid_source_checkout_reports_missing_marker_details` |
| Unreadable source path reports `SOURCE_CHECKOUT_INVALID` | Complete | `test_unreadable_source_checkout_reports_source_checkout_invalid` |
| Stored source metadata is revalidated and stale metadata fails | Complete | `test_stale_source_checkout_metadata_fails_without_package_fallback` |
| No fallback to package assets after explicit source metadata | Complete | Stale metadata path raises `SOURCE_CHECKOUT_ASSETS_STALE` with `fallback_used: false` instead of returning a package/default asset root |

## Files Changed

- `plans/T02_HOST_SETUP_CONFIG_SOURCE_ASSETS_PLAN.md`
- `src/awf/host_setup/__init__.py`
- `src/awf/host_setup/config.py`
- `src/awf/host_setup/source_assets.py`
- `tests/unit/service/test_host_setup_config.py`

## TDD Evidence

Initial focused test run after adding tests failed during collection because
`awf.host_setup` did not exist:

```text
ModuleNotFoundError: No module named 'awf.host_setup'
```

Implementation then added the smallest new package surface required by the
tests and T02 contract.

## Focused Verification

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/host_setup tests/unit/service/test_host_setup_config.py
uv run --python 3.12 --extra dev mypy src/awf/host_setup
```

Results:

```text
7 passed in 0.46s
All checks passed!
Success: no issues found in 3 source files
```

Full AWF/GitHub validation, full coverage gates, broad unit suites, frontend
builds, OpenAPI drift checks, push, PR creation, and merge monitoring are owned
by AWF/GitHub after agent completion and were intentionally not run in this
workspace phase.

## Remaining Gaps

None for T02.
