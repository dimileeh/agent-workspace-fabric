# Console Theme CI Fix Plan

## Problem Statement and Scope

PR #253 has a failing GitHub Actions `console` job. The focused Playwright
repro matches CI:

- `task details modal scrolls long prompts without moving the dashboard`
  scrolls the modal body, but `window.scrollY` jumps from the pre-modal
  dashboard offset to `0`.
- `mobile theme screenshots cover dashboard, workspace, and logs views` times
  out because the selector looks for `Dark theme verification workspace`, which
  is only present in the workspace detail fixture, not the visible workspace
  list item.

Scope is limited to the console theme/accessibility surface and its regression
coverage. Do not weaken or skip the failing checks.

## Requirements Checklist

- Preserve the dashboard scroll position while a task details modal is open.
- Keep wheel scrolling contained in the task details modal content.
- Keep the mobile screenshot test selecting the actual visible workspace row.
- Do not change git branches or push.
- Commit the local fix with a conventional CI-fix message.

## Implementation Steps

1. Update the task details scroll lock to preserve the current scroll offset
   while disabling background scrolling.
2. Update the mobile test selector to target the visible workspace list title
   from the mock overview fixture.
3. Run the focused Playwright repro for the two failing tests.
4. Run the console lint, typecheck, build, and full browser test commands.
5. Recheck PR checks and commit the local fix.

## Verification Commands and Pass Criteria

- `npm --prefix apps/console run test:browser -- tests/theme-accessibility.spec.ts -g "task details modal scrolls|mobile theme screenshots"`
  passes.
- `npm --prefix apps/console run lint` passes.
- `npm --prefix apps/console run typecheck` passes.
- `npm --prefix apps/console run build` passes.
- `npm --prefix apps/console run test:browser` passes.
- `gh pr checks 253 --json name,state,bucket,link,startedAt,completedAt,workflow`
  shows no completed GitHub Actions failures for the PR head after the fix is
  available to CI, or any remaining failure is explicitly inspected.

## Assumptions/Changes

- Local focused repro showed the original modal scroll test clicked an offscreen
  first `Details` button after scrolling to the bottom, causing Playwright to
  auto-scroll the dashboard back to the top before opening the modal. The
  regression should instead start from a nonzero scroll offset where the target
  details button remains in the viewport, so the assertion measures modal scroll
  containment rather than test-driver auto-scroll.
