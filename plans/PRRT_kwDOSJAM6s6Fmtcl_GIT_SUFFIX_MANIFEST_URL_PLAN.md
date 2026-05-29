# PRRT_kwDOSJAM6s6Fmtcl Git Suffix Manifest URL Plan

## Problem Statement and Scope

An unresolved review thread reports that `scripts/generate_install_manifest.py`
preserves a trailing `.git` suffix when normalizing GitHub repository clone URLs.
That suffix then appears in generated release artifact URLs and the manifest
source repository.

Scope is limited to repository URL normalization in the install manifest
generator and focused regression coverage for `.git` clone URL inputs.

## Requirements Checklist

- Add regression coverage proving `https://github.com/<owner>/<repo>.git` is
  accepted but normalized to `https://github.com/<owner>/<repo>`.
- Ensure generated artifact URLs do not contain the `.git` suffix.
- Preserve existing validation for HTTPS GitHub owner/repository URLs and
  mutable path rejection.
- Run only focused local validation; AWF/GitHub own broad validation after the
  agent phase.

## Implementation Steps

1. Add a focused unit test that runs the manifest generator with a `.git`
   repository URL and asserts source/artifact URLs are suffix-free.
2. Confirm the new test fails against the current implementation.
3. Strip a trailing `.git` suffix during repository URL normalization after
   trimming trailing slashes and before parsing/validating.
4. Re-run the focused manifest generator test file or narrower relevant tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_normalizes_git_suffix_from_repository_clone_url -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  passes after implementation.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
