# Review 4596340227 AWF Version Exit Plan

## Problem Statement and Scope

PR review comment `issue:4596340227` points out that `packaging/install.sh`
parses `awf --version` stdout even when the command exits non-zero. That allows
the default-install PATH fallback to pass the version gate when a candidate
binary prints the expected version but reports failure.

Scope is limited to installer PATH-fallback verification and its regression
coverage.

## Requirements Checklist

- Add a regression test for a PATH `awf` whose `--version` prints the install
  version but exits non-zero.
- Ensure `awf_version_matches_install` rejects non-zero `awf --version` probes
  before parsing stdout.
- Preserve existing successful matching-version fallback behavior.
- Run focused installer verification only; broad AWF/GitHub validation remains
  owned by AWF after agent completion.

## Implementation Steps

1. Add the failing installer regression test using the existing harness
   `version_rc` support.
2. Run the targeted new test and confirm it fails against current behavior.
3. Update `awf_version_matches_install` to require a successful `--version`
   command before token normalization/comparison.
4. Re-run the targeted regression test and a focused neighboring installer test
   that proves matching PATH fallback still succeeds.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py -q -k "nonzero_path_awf_version_probe or accepts_matching_path"`
  - Passes after implementation.
  - Before implementation, the new non-zero version regression should fail.
