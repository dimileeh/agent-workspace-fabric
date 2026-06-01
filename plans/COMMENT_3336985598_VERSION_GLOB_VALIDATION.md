# Comment 3336985598 Version Globbing Validation

Plan reference: `plans/COMMENT_3336985598_VERSION_GLOB_PLAN.md`

## Requirement Status

- Complete: Replaced `for token in $reported` in `packaging/install.sh` with a
  `read` loop over whitespace-normalized tokens, preventing pathname expansion.
- Complete: Added
  `tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_glob_expansion_in_path_awf_version`
  to prove glob characters in `awf --version` output cannot match cwd filenames.
- Complete: Ran focused installer verification only.
- Complete: Broad AWF/GitHub validation, coverage gates, and CI-equivalent
  commands were not run during the agent phase; AWF/GitHub owns them after
  completion.

## Evidence

- Changed `packaging/install.sh`.
- Changed `tests/unit/installer/test_install_sh_install.py`.
- Added this plan/validation pair for the review thread.

## Commands

- Failing-first evidence before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_glob_expansion_in_path_awf_version -q`
  failed because the installer returned success for a stale PATH `awf` whose
  version output was `*` and the cwd contained `0.1.0`.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_glob_expansion_in_path_awf_version -q`
  passed.
- Passing syntax check:
  `bash -n packaging/install.sh`
- Passing neighboring behavior slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_stale_path_awf_when_bin_dir_empty tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_glob_expansion_in_path_awf_version tests/unit/installer/test_install_sh_install.py::test_default_install_accepts_matching_path_awf_when_bin_dir_empty -q`
- Passing whitespace check:
  `git diff --check -- packaging/install.sh tests/unit/installer/test_install_sh_install.py plans/COMMENT_3336985598_VERSION_GLOB_PLAN.md`
