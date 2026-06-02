# Review issue 4596340227 install version invariant plan

## Problem Statement and Scope

Greptile noted that `packaging/install.sh` assigns `INSTALL_VERSION` before the
`VERSION_MISMATCH` guard in `verify_artifact_name`. The current process exits on
that guard, so this is operationally harmless, but moving the assignment after
the guard keeps `INSTALL_VERSION` reserved for a wheel version that has been
accepted by this run.

Scope is limited to the installer invariant and focused installer verification.
No branch changes, pushes, broad AWF validation, full coverage, or frontend
builds.

## Requirements Checklist

- Move the `INSTALL_VERSION="$expected_version"` assignment until after the
  wheel version has been compared with the expected manifest or pinned version.
- Preserve the legacy unpinned, versionless-manifest path that accepts the wheel
  version as the version of record.
- Preserve existing installer behavior and regression coverage for mismatched
  wheel versions and PATH fallback verification.
- Commit the focused fix locally with a conventional commit message.

## Implementation Steps

1. Inspect the current `verify_artifact_name` ordering and nearby tests.
2. Move the assignment after the `VERSION_MISMATCH` guard.
3. Run targeted installer tests that cover artifact version mismatch, accepted
   version paths, and the PATH fallback identity/runnability behavior.
4. Record focused validation evidence in a validation document.
5. Stage only the changed files and commit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_artifact_name.py tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_stale_path_awf_when_bin_dir_empty tests/unit/installer/test_install_sh_install.py::test_default_install_accepts_matching_path_awf_when_bin_dir_empty tests/unit/installer/test_install_sh_install.py::test_default_install_matching_path_awf_that_fails_help_is_not_reachable -q`

Pass criteria: all targeted tests pass. Full AWF/GitHub validation is managed by
AWF after agent completion per the workspace contract.
