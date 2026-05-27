# PR 289 Review Comment 4556565363 Plan

## Problem Statement

Review-level comment `issue:4556565363` raised two companion-service graph
concerns:

- `ComposeStackLauncher` calls `validate_companion_service_graph` even when a
  workspace has no companions, which can reject profile-service dependencies
  whose targets do not declare healthchecks.
- `_companion_service_dependency_cycle` repeats duplicate-name and
  profile-collision checks that the public validator already performs before
  cycle detection.

## Scope

- Keep stack-launcher validation behavior if it matches the existing Compose
  dependency contract and current tests.
- Make the intentional profile-only preflight behavior explicit with focused
  regression coverage.
- Remove redundant private collision checks from companion cycle detection while
  preserving public validation reason codes.
- Do not run broad AWF/GitHub-owned validation; AWF owns broad validation after
  agent completion.

## Requirements Checklist

- Preserve existing profile-only stack-launch validation instead of weakening
  existing policy-backed tests merely to satisfy review feedback.
- Add focused coverage showing a companion-free profile with a dependency on a
  non-healthchecked target fails with
  `COMPANION_SERVICE_DEPENDENCY_UNHEALTHY` before Compose launch.
- Remove unreachable duplicate-name and profile-collision raises from
  `_companion_service_dependency_cycle`.
- Preserve public duplicate companion name, profile collision, unknown
  dependency, unhealthy dependency, and cycle validation behavior.
- Validate with targeted unit tests only.

## Implementation Steps

1. Add the stack-launcher regression test for companion-free unhealthy profile
   dependencies and run it to confirm the current behavior is captured.
2. Remove redundant collision checks from `_companion_service_dependency_cycle`.
3. Run focused companion-service and stack-launcher tests that cover the changed
   behavior.
4. Create `plans/PR_289_REVIEW_COMMENT_4556565363_VALIDATION.md` with
   requirement status and focused evidence.

## Follow-up Scope: Dockerfile Checksums and GC Partial Worktree Cleanup

The same review-level comment now carries two follow-up observations:

- The agent runtime image downloads the GitHub CLI checksum manifest from the
  same release endpoint as the `.deb`, so the checksum only proves transfer
  integrity during image build.
- GC attempts every primary and companion worktree removal independently, but
  a single failure still returns one workspace-level `failed` result and causes
  the caller to skip filesystem deletion for every worktree path, including
  removals that succeeded.

## Follow-up Requirements Checklist

- Embed the pinned GitHub CLI amd64 and arm64 SHA256 hashes in
  `docker/agent-runtime.Dockerfile` as build arguments.
- Remove the runtime checksum-manifest fetch from the Dockerfile and keep the
  `.deb` checksum verification before install.
- Add/update focused Dockerfile unit coverage for the embedded-hash contract.
- Add a GC regression proving successfully removed worktree paths can be
  deleted even when another primary or companion worktree removal fails.
- Preserve partial-cleanup reporting and retry behavior for failed worktree
  removals.
- Run only focused local checks; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Follow-up Implementation Steps

1. Update the Dockerfile GitHub CLI install block to select
   `GH_AMD64_SHA256` or `GH_ARM64_SHA256` based on `dpkg --print-architecture`.
2. Update `tests/unit/test_agent_runtime_dockerfile.py` to reject the manifest
   fetch and assert the embedded hashes are checked before package install.
3. Extend the GC worktree-removal result with per-target success/failure
   details and use those details to skip only failed worktree paths.
4. Update focused GC tests for partial companion failure semantics and result
   serialization.
5. Record focused verification evidence in
   `plans/PR_289_REVIEW_COMMENT_4556565363_VALIDATION.md`.
