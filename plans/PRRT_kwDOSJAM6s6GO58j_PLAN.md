# PRRT_kwDOSJAM6s6GO58j Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GO58j` reports a remaining race in companion
image launch: AWF revalidates a pre-built local companion image before
`docker compose up`, but GC can prune the tag after that check and before
Compose creates the companion container. A vanished local-only tag rendered as
`image:` plus `pull_policy: never` can still fail instead of falling back to an
inline build.

Scope is limited to the stack launcher behavior and focused unit coverage for
that race. Broad AWF/GitHub validation remains owned by AWF after agent
completion.

## Requirements Checklist

- Preserve the existing pre-launch companion image revalidation behavior.
- Detect a `docker compose up` failure that specifically reports a missing
  pre-built companion image tag rendered in the launch spec.
- Retry `docker compose up` once with the missing companion image cleared so
  Compose can build inline from the existing companion build context.
- Do not retry unrelated Compose failures or profile service image failures.
- Add or update a regression test that fails without the post-revalidation retry.
- Run focused tests for the touched behavior only.

## Implementation Steps

1. Add a targeted unit test in `tests/unit/node/test_stack_launcher_companion_images.py`
   that simulates the first `compose.up` failing with a missing pre-built
   companion image after revalidation and asserts the second launch clears the
   companion image.
2. Update `src/awf/node/stack_launcher.py` to classify missing-image Compose
   failures by matching missing-image output against pre-built companion image
   tags in the current spec.
3. On a matching failure, clear only the missing companion images and retry
   `compose.up` once with `wait=True`.
4. Keep existing Docker-unavailable error mapping intact for both the initial
   attempt and any retry.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6GO58j_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`

Pass criteria: the focused companion-image stack launcher tests pass, including
the new post-revalidation retry regression. Full AWF/GitHub validation is not
run in the agent phase per workspace contract.
