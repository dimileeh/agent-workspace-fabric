# Getting Started Runnable Startup Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FgPwq` reports that
`docs/GETTING_STARTED.md` recommends `awf setup` and `awf start` as copy-paste
startup commands even though those command surfaces currently exit with
`AWF_SETUP_PLACEHOLDER` and `AWF_START_PLACEHOLDER`.

Scope is limited to the public Getting Started startup guidance and focused
docs regression coverage. No CLI behavior changes are planned.

## Requirements Checklist

- Replace the Getting Started first-run copy-paste block with the current
  runnable startup path using `awf service bootstrap` followed by
  `awf service status --format pretty`.
- Keep `awf setup` and `awf start` mentioned only as reserved future command
  surfaces with their placeholder reason codes.
- Preserve the existing project onboarding guidance for `awf init <path>`.
- Update focused docs tests so Getting Started cannot regress to presenting
  `awf setup` or `awf start` as runnable startup commands.
- Run only targeted validation for the changed docs test; broad AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a failing docs regression test for the Getting Started startup section.
2. Update `docs/GETTING_STARTED.md` to match the current runnable Quickstart
   path and clarify placeholder command surfaces.
3. Run the focused docs test.
4. Record validation evidence in a companion validation file.
