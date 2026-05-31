# ISSUE-336: DNS base-fetch errors classified as terminal instead of transient

## Problem
`_is_transient_base_fetch_error` in helpers.py classifies DNS resolution failures
(e.g. `Could not resolve host: github.com`) as non-transient, causing the PR monitor
to terminally fail the workspace with `GIT_FETCH_BASE_FAILED`. DNS blips are transient.

## Root cause
`_TRANSIENT_GITHUB_ERROR_MARKERS` in constants.py lacks DNS-resolution markers.
The classifier checks non-transient markers first (none match), then transient markers
(none match), so it falls through to `False` → terminal failure.

## Fix
1. Add DNS-resolution failure markers to `_TRANSIENT_GITHUB_ERROR_MARKERS`:
   - `could not resolve host`
   - `temporary failure in name resolution`
   - `name or service not known`
   - `could not resolve proxy`
2. Verify no collision with existing non-transient markers
   (`could not resolve to a repository`, `could not resolve to a node`).
3. Add regression tests for DNS-transient classification and non-transient non-regression.

## Files to change
- `src/awf/runtime/pr_monitor_runner/constants.py` — add markers
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py` — extend existing test

## Non-goals
- No new retry path or budget changes
- No changes to backoff logic
