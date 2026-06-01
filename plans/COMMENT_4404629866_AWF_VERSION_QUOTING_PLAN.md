# Comment 4404629866 AWF Version Quoting Plan

## Problem Statement and Scope

CodeRabbit flagged that `InstallerHarness.add_awf()` embeds the `version`
parameter in a single-quoted shell string. A future test version containing a
single quote would produce a syntactically invalid stub script.

Scope is limited to the installer test harness and a focused regression for the
stub output.

## Requirements

- Verify the finding against current code.
- Shell-quote the generated `awf --version` output safely.
- Add a focused regression covering a version string with a single quote.
- Run only targeted installer test commands for the changed behavior.
- Leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Update `tests/unit/installer/conftest.py` to quote the full `awf <version>`
   output before embedding it in the generated shell behavior.
2. Add a focused unit test that executes an `awf` stub with a single quote in
   the version and verifies the exact `--version` output.
3. Run the focused regression test.

## Verification

Pass criteria:

- The focused regression passes.
- No broad repository validation or coverage gate is run inside this agent
  phase.
