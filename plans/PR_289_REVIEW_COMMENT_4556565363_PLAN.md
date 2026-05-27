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
