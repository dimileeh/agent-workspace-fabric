# PRRT_kwDOSJAM6s6Fox3b Branch Manifest Provenance Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6Fox3b` reports that manual publish workflow
dispatches from branch refs can generate release manifests whose `source.tag`
and artifact URLs point at `v<version>` while the artifacts were built from
`GITHUB_SHA` on the branch. If the tag is missing or points at another commit,
the manifest records incorrect GitHub Release provenance.

Scope is limited to `scripts/generate_install_manifest.py`, focused manifest
generator tests, and this plan/validation record. Protected workflow files are
not edited.

## Requirements Checklist

- Preserve normal local manifest generation.
- Preserve GitHub Actions tag-ref manifest generation for the exact release tag.
- Allow manual `workflow_dispatch` branch refs only when the requested release
  tag resolves locally to the same commit as `GITHUB_SHA`.
- Skip manifest generation and remove stale output for manual branch dispatches
  when the tag is missing, cannot be verified, or resolves to a different
  commit.
- Keep non-dispatch branch refs skipped.
- Run focused manifest tests and focused lint only; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add focused regressions for unsafe manual branch dispatch provenance and the
   safe same-commit branch-dispatch case.
2. Run the unsafe branch-dispatch regression first to confirm it fails on the
   current implementation.
3. Update `_github_actions_skip_reason` to verify workflow-dispatch branch refs
   against `refs/tags/<tag>` and `GITHUB_SHA` before allowing manifest output.
4. Re-run the focused regressions and manifest generator test file.
5. Run focused lint for the changed Python files.
6. Record validation evidence in the matching validation document and commit the
   scoped changes locally.

## Verification Commands and Pass Criteria

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_generator_skips_workflow_dispatch_branch_ref_without_verified_tag -q`
  fails before implementation because the current branch dispatch path writes a
  manifest.
- Green focused tests: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  passes after implementation.
- Focused lint: `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  passes.

Full repository tests, coverage gates, frontend builds, OpenAPI drift checks,
and CI-equivalent validation are intentionally left to AWF/GitHub after agent
completion.
