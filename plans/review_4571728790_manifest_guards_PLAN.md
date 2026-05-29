# Review 4571728790 Manifest Guards Plan

## Problem Statement and Scope

PR review comment `issue:4571728790` flagged release manifest edge cases around
GitHub Actions tag gating and repository URL normalization. The in-scope code is
`scripts/generate_install_manifest.py` and its focused unit tests.

The workflow-level suggestion would require editing `.github/workflows/publish.yml`,
which is a protected workflow file and is not necessary for the script defects
below.

## Requirements Checklist

- Add a regression test proving `GITHUB_ACTIONS=true` with both `GITHUB_REF_TYPE`
  and `GITHUB_REF_NAME` absent skips manifest generation instead of bypassing the
  release tag guard.
- Add a regression test proving a GitHub repository URL with doubled path slashes
  is normalized to the canonical `https://github.com/<owner>/<repo>` form.
- Implement the smallest generator changes that satisfy those tests without
  weakening existing validation behavior.
- Do not edit protected workflow files.
- Run targeted tests for the manifest generator only; leave broad AWF/GitHub
  validation to AWF after agent completion.

## Implementation Steps

1. Update `tests/unit/scripts/test_generate_install_manifest.py` with failing
   coverage for the missing Actions ref guard and doubled-slash repository URL.
2. Run the targeted tests to confirm the new expectations fail.
3. Update `scripts/generate_install_manifest.py` to require an Actions ref and
   reconstruct the normalized repository URL from parsed owner/repo path parts.
4. Re-run the targeted manifest generator tests.
5. Save a validation document with requirement status and focused evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  must pass after implementation.
- Full repository validation, coverage gates, and GitHub workflow validation are
  intentionally not run in the agent phase per the AWF workspace contract.
