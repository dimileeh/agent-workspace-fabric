# PRRT_kwDOSJAM6s6Dh2r Plan

## Problem Statement and Scope

The preserved active execution recovery path derives a missing PR number from
`workspace.pr_url` before deciding whether it can reattach an existing PR
monitor. The current parser accepts canonical PR URLs and PR subpaths, but it
does not accept valid GitHub PR URLs where the PR number is followed directly by
a query string or fragment.

Scope is limited to PR number extraction and tests for the recovery-relevant
helpers.

## Requirements Checklist

- Accept PR URLs whose `/pull/<number>` segment is followed by `?query`.
- Accept PR URLs whose `/pull/<number>` segment is followed by `#fragment`.
- Preserve existing support for canonical, trailing slash, and subpath PR URLs.
- Preserve rejection of non-PR URLs and non-numeric PR numbers.
- Keep the duplicate worker and executor extraction helpers consistent.

## Implementation Steps

1. Add regression coverage for query and fragment PR URLs.
2. Confirm the new worker regression fails against the current parser.
3. Update the PR number regex to accept `?` and `#` as valid boundaries after
   the PR number.
4. Run the targeted unit tests that cover the changed helpers.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<target> -q`
  fails before the parser change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<target> tests/unit/control/test_executor.py::TestPrNumberExtraction -q`
  passes after implementation.
