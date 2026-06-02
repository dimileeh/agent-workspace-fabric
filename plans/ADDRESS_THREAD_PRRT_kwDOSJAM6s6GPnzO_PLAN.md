# Address PRRT_kwDOSJAM6s6GPnzO Plan

## Problem Statement And Scope

The installer test harness cannot model an `awf` executable that reports the
freshly installed version successfully on `--version` but fails the later
`--help` runnability check. This leaves the default PATH fallback
`AWF_NOT_REACHABLE` branch without direct regression coverage.

Scope is limited to installer unit-test fixtures and a focused regression test.

## Requirements Checklist

- Add a backward-compatible `InstallerHarness.add_awf` option that separates the
  `--help` exit status from the `--version` exit status.
- Add a regression test for a matching-version PATH `awf` that is accepted by
  `awf_version_matches_install` and then rejected by the `--help` check.
- Preserve existing `add_awf(rc=...)` behavior for current tests.
- Run focused installer unit tests only; broad AWF/GitHub validation remains
  owned by AWF after agent completion.

## Implementation Steps

1. Add the failing regression test using the intended `help_rc` harness API.
2. Run the new focused test to confirm the existing harness cannot satisfy it.
3. Implement `help_rc` in `tests/unit/installer/conftest.py`.
4. Run the new regression and nearby installer tests that exercise `rc`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_default_install_matching_path_awf_that_fails_help_is_not_reachable -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_found_binary_not_runnable_reports_broken_not_path tests/unit/installer/test_install_sh_install.py::test_default_install_binary_verified_even_when_path_awf_is_broken tests/unit/installer/test_install_sh_install.py::test_install_dir_binary_verified_even_when_path_awf_is_broken -q`
  passes.
