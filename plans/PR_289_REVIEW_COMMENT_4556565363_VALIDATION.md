# PR 289 Review Comment 4556565363 Validation

Plan reference:
`plans/PR_289_REVIEW_COMMENT_4556565363_PLAN.md`

## Requirement Status

- Complete: Preserved existing profile-only stack-launch validation. The new
  regression test documents that companion-free profiles with `depends_on`
  targets lacking healthchecks fail before Compose launch with
  `COMPANION_SERVICE_DEPENDENCY_UNHEALTHY`.
- Complete: Removed unreachable duplicate-name and profile-collision raises
  from `_companion_service_dependency_cycle`; public validation remains the
  entry point for those reason codes.
- Complete: Preserved public duplicate companion name, profile collision,
  unknown dependency, unhealthy dependency, and cycle behavior through focused
  companion graph tests.
- Complete: Used targeted local checks only. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_stack_launcher.py`
- `plans/PR_289_REVIEW_COMMENT_4556565363_PLAN.md`
- `plans/PR_289_REVIEW_COMMENT_4556565363_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_preflights_profile_dependencies_without_companions -q`
  - Passed: `1 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  - Passed: `16 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q`
  - Passed: `25 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_stack_launcher.py`
  - Passed: `All checks passed!`

## Gaps

None.

## Follow-up Validation: Dockerfile Checksums and GC Partial Worktree Cleanup

Follow-up plan reference:
`plans/PR_289_REVIEW_COMMENT_4556565363_PLAN.md`

### Requirement Status

- Complete: Embedded the pinned GitHub CLI amd64 and arm64 SHA256 hashes in
  `docker/agent-runtime.Dockerfile` as build arguments.
- Complete: Removed the runtime checksum-manifest fetch from the Dockerfile;
  the image build now verifies the downloaded `.deb` against the embedded
  architecture-specific hash before install.
- Complete: Updated focused Dockerfile unit coverage to require embedded
  hashes and reject the release checksum-manifest fetch.
- Complete: Added GC regression coverage proving successfully removed primary
  and companion worktree paths are deleted even when another companion removal
  fails.
- Complete: Preserved partial-cleanup reporting and retry behavior. Failed
  worktree targets still keep their filesystem paths and mark the GC execution
  partial, while successful worktree targets can be deleted in the same pass.
- Complete: Used targeted local checks only. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

### Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `src/awf/service/gc.py`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `tests/unit/service/test_gc_more2.py`
- `plans/PR_289_REVIEW_COMMENT_4556565363_PLAN.md`
- `plans/PR_289_REVIEW_COMMENT_4556565363_VALIDATION.md`

Focused pre-implementation failures:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_pinned_github_cli_from_release_asset -q`
  failed because the Dockerfile did not yet declare embedded
  `GH_AMD64_SHA256` / `GH_ARM64_SHA256` values.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py::test_gc_partial_worktree_remove_deletes_successful_worktree_paths tests/unit/service/test_gc_more2.py::test_default_worktree_remover_continues_after_companion_failure tests/unit/service/test_gc_more2.py::test_worktree_remove_result_to_dict_with_error -q`
  failed at collection because `WorkspaceGCWorktreeRemoveTargetResult` did not
  exist yet.

Focused passing checks:

- `curl -fsSL https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_checksums.txt | awk '$2 == "gh_2.92.0_linux_amd64.deb" || $2 == "gh_2.92.0_linux_arm64.deb" { print }'`
  returned the pinned amd64 and arm64 hashes.
- `curl -fsSL https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_amd64.deb | sha256sum`
  matched `8f8212b1a9cec261a8839e0893168f50d3fc70f095da257feef4229234cefdf8`.
- `curl -fsSL https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_arm64.deb | sha256sum`
  matched `34d620b7c884774ed86236541535170889fda0b99aafbdab8b69c7d458b5ca6b`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_pinned_github_cli_from_release_asset -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py::test_gc_partial_worktree_remove_deletes_successful_worktree_paths tests/unit/service/test_gc_more2.py::test_default_worktree_remover_continues_after_companion_failure tests/unit/service/test_gc_more2.py::test_worktree_remove_result_to_dict_with_error -q`
  passed: `3 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py tests/unit/service/test_gc_more2.py tests/unit/test_agent_runtime_dockerfile.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/gc.py tests/unit/service/test_gc_more2.py tests/unit/test_agent_runtime_dockerfile.py`
  passed: `3 files already formatted`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passed: `6 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -q`
  passed: `35 passed`.
- `uv run --python 3.12 --extra dev mypy src/awf/service/gc.py`
  passed.

### Follow-up Gaps

None.
