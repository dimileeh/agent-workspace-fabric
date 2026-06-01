# Validation: verify_awf PATH fallback identity check

## Plan Check

- Added regression coverage for issues #331 and #340:
  - stale/unrelated PATH `awf` with a mismatched `--version` is rejected when
    the default install produced no binary in the resolved bin dir.
  - matching PATH `awf` is still accepted when the default bin-dir prediction
    missed the actual linked executable.
- Kept the existing resolved-bin-dir branch and explicit `--install-dir`
  missing-binary behavior unchanged.
- Preserved the existing `$resolved --help` runnability check and PATH-advice
  failure path.
- Added version identity plumbing from the verified wheel so the default PATH
  fallback compares against the release this installer run just installed.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py -q -k 'default_install_rejects_stale_path_awf_when_bin_dir_empty or default_install_accepts_matching_path_awf_when_bin_dir_empty'`
  - Before implementation: expected failure reproduced for the stale PATH case.
  - After implementation: `2 passed, 31 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/installer -q`
  - `114 passed`.
- `bash -n packaging/install.sh`
  - passed.
- `uv run --python 3.12 --extra dev ruff check .`
  - `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check .`
  - `947 files already formatted`.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - `Success: no issues found in 314 source files`.
- `uv run --python 3.12 --extra dev pytest`
  - `9859 passed, 7 skipped in 3232.77s (0:53:52)`.
  - Skips were Docker/Compose-dependent integration tests in this workspace.

## Result

The implementation satisfies the saved plan and acceptance criteria. Full AWF
push/PR lifecycle remains owned by AWF after agent completion.
