# Review 4596340227 AWF Version Exit Validation

Plan reference: `REVIEW_4596340227_AWF_VERSION_EXIT_PLAN.md`

## Requirement Status

- Add a regression test for a PATH `awf` whose `--version` prints the install
  version but exits non-zero: Complete.
- Ensure `awf_version_matches_install` rejects non-zero `awf --version` probes
  before parsing stdout: Complete.
- Preserve existing successful matching-version fallback behavior: Complete.
- Run focused installer verification only; broad AWF/GitHub validation remains
  owned by AWF after agent completion: Complete.

## Evidence

Files changed:

- `packaging/install.sh`
- `tests/unit/installer/test_install_sh_install.py`
- `plans/REVIEW_4596340227_AWF_VERSION_EXIT_PLAN.md`

Focused verification:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py -q -k nonzero_path_awf_version_probe`
  failed because the installer returned success for the new regression.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py -q -k "nonzero_path_awf_version_probe or accepts_matching_path"`
  passed with `2 passed, 35 deselected`.

Broad validation was not run in the agent phase; AWF/GitHub owns broad
validation, provenance, logs, timeouts, and merge gating after completion.

## Gaps

None.
