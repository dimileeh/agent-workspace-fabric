# PRRT_kwDOSJAM6s6Fmtcl Git Suffix Manifest URL Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Fmtcl_GIT_SUFFIX_MANIFEST_URL_PLAN.md`

## Requirement Status

- Complete: Regression coverage proves
  `https://github.com/<owner>/<repo>.git` is accepted and normalized to
  `https://github.com/<owner>/<repo>`.
- Complete: Generated artifact URLs no longer contain the `.git` suffix before
  `/releases/download/`.
- Complete: Existing repository URL validation remains intact; the focused
  manifest generator test file passes.
- Complete: Validation stayed focused. Full AWF/GitHub validation is managed by
  AWF after agent completion per the workspace contract.

## Evidence

Files changed:

- `scripts/generate_install_manifest.py`
- `tests/unit/scripts/test_generate_install_manifest.py`
- `plans/PRRT_kwDOSJAM6s6Fmtcl_GIT_SUFFIX_MANIFEST_URL_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Fmtcl_GIT_SUFFIX_MANIFEST_URL_VALIDATION.md`

Commands run:

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_normalizes_git_suffix_from_repository_clone_url -q`
  failed because the manifest source repository preserved `.git`.
- Passing regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_normalizes_git_suffix_from_repository_clone_url -q`
- Passing focused unit file:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`

## Gaps

None.
