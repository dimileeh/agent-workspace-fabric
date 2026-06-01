# Address PRRT_kwDOSJAM6s6GPnzO Validation

Plan reference: `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GPnzO_PLAN.md`

## Requirement Status

- Add a backward-compatible `InstallerHarness.add_awf` option that separates the
  `--help` exit status from the `--version` exit status: Complete.
- Add a regression test for a matching-version PATH `awf` that is accepted by
  `awf_version_matches_install` and then rejected by the `--help` check:
  Complete.
- Preserve existing `add_awf(rc=...)` behavior for current tests: Complete.
- Run focused installer unit tests only; broad AWF/GitHub validation remains
  owned by AWF after agent completion: Complete.

## Evidence

Files changed:

- `tests/unit/installer/conftest.py`
- `tests/unit/installer/test_install_sh_install.py`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GPnzO_PLAN.md`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GPnzO_VALIDATION.md`

Focused checks:

- Confirmed the new regression failed before fixture implementation with
  `TypeError: InstallerHarness.add_awf() got an unexpected keyword argument
  'help_rc'`.
- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_default_install_matching_path_awf_that_fails_help_is_not_reachable tests/unit/installer/test_install_sh_install.py::test_found_binary_not_runnable_reports_broken_not_path tests/unit/installer/test_install_sh_install.py::test_default_install_binary_verified_even_when_path_awf_is_broken tests/unit/installer/test_install_sh_install.py::test_install_dir_binary_verified_even_when_path_awf_is_broken -q`
  passed with 4 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/installer/conftest.py tests/unit/installer/test_install_sh_install.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
