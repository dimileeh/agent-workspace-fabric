# Review PRRT_kwDOSJAM6s6ESXPf Guided Write Confirmation Plan

## Problem

The PR review reports that `awf init <path> --write-profile --guided` writes
`.awf/workspace.yml` even when the user declines the guided write confirmation.
That would make an interactive "no" answer ineffective and mutate files
unexpectedly.

## Scope

- Verify and fix only the guided project-onboarding write decision in
  `src/awf/cli/main.py`.
- Add a focused regression test in `tests/unit/cli/test_init.py`.
- Do not run broad AWF/GitHub-owned validation; AWF handles that after agent
  completion.

## Requirements

- `--write-profile --guided` must honor a final guided write answer of "no".
- Non-guided `--write-profile --yes` must continue to write.
- Guided writes still occur when the guided confirmation is accepted.
- The change must be covered by a targeted unit test.

## Implementation Steps

1. Add a failing unit test for `--write-profile --guided` with a declined write
   confirmation.
2. Change the project-onboarding write decision so guided mode uses the final
   guided confirmation.
3. Run only focused tests and lint for the touched behavior/files.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k "guided or write_profile"`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
