# Console Theme CI Fix Validation

Plan reference: `CONSOLE_THEME_CI_FIX_PLAN.md`

## Requirement Status

- Complete: Preserve the dashboard scroll position while a task details modal is
  open. `TaskDetailsModal` now preserves the current `window.scrollY` while
  locking body scroll, and the Playwright regression starts from a nonzero
  visible scroll offset.
- Complete: Keep wheel scrolling contained in the task details modal content.
  The focused Playwright regression verifies `task-details-scroll` advances
  while `window.scrollY` remains unchanged.
- Complete: Keep the mobile screenshot test selecting the actual visible
  workspace row. The selector now targets the mock overview title rendered in
  the list.
- Complete: Do not change git branches or push. Work stayed on the AWF-managed
  current branch and no push/rebase/branch command was run.
- Complete: Commit the local fix with a conventional CI-fix message. The local
  commit for this fix includes the code, plan, and validation docs.

## Evidence

Files changed:

- `apps/console/components/console-dashboard.tsx`
- `apps/console/tests/theme-accessibility.spec.ts`
- `plans/CONSOLE_THEME_CI_FIX_PLAN.md`
- `plans/CONSOLE_THEME_CI_FIX_VALIDATION.md`

Commands run:

- `npm --prefix apps/console ci` passed.
- `npm --prefix apps/console exec playwright install --with-deps chromium`
  passed.
- `npm --prefix apps/console run test:browser -- tests/theme-accessibility.spec.ts -g "task details modal scrolls|mobile theme screenshots"`
  failed before the fix with the same two CI failures, then passed after the
  fix: 2 passed.
- `npm --prefix apps/console run lint` passed.
- `npm --prefix apps/console run typecheck` passed.
- `npm --prefix apps/console run build` passed.
- `npm --prefix apps/console run test:browser` passed: 14 passed.
- `git diff --check` passed.

Remote check status:

- `gh pr checks 253 --json name,state,bucket,link,startedAt,completedAt,workflow`
  still reports the old remote `console` failure for run `25909783523` because
  this local fix has not been pushed by AWF yet.
- The same old run still has `python-full-coverage` in progress, while
  `lint-and-type` and `release-artifacts` are successful.
