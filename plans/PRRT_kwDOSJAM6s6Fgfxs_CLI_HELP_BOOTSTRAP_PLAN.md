# PRRT_kwDOSJAM6s6Fgfxs CLI Help Bootstrap Plan

## Problem Statement And Scope

Unresolved PR review thread `PRRT_kwDOSJAM6s6Fgfxs` reports that AWF CLI help
still recommends the placeholder `awf setup` then `awf start` first-run path.
Those commands currently exit non-zero with placeholder reason codes, while the
current runnable local Core startup path is `awf service bootstrap`.

Scope is limited to user-facing CLI help guidance and focused regression tests.

## Requirements Checklist

- Top-level `awf --help` recommends `awf service bootstrap`, then `awf init <path>`.
- `awf init --help` recommends `awf service bootstrap`, then `awf init <path>`.
- Other shared first-path help snippets do not recommend placeholder setup/start
  as the current first path.
- Placeholder command behavior for `awf setup` and `awf start` remains unchanged.
- Add/update focused regression tests before implementation.
- Run only targeted validation; broad AWF/GitHub validation remains AWF-owned
  after agent completion.

## Implementation Steps

1. Update focused CLI help tests to require `awf service bootstrap` guidance and
   reject the stale `awf setup` / `awf start` first-path sentence.
2. Run the focused help tests to confirm they fail on the current implementation.
3. Update the shared CLI help snippets in the affected CLI modules.
4. Re-run the focused help tests.
5. Save validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestCliHelp -q`
  - Passes after implementation.
  - Before implementation, fails because stale help still recommends
    `awf setup` / `awf start`.
